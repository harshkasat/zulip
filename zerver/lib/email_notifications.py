# See https://zulip.readthedocs.io/en/latest/subsystems/notifications.html

import logging
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import timedelta
from email.headerregistry import Address
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import lxml.html
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_backends
from django.utils.timezone import now as timezone_now
from django.utils.translation import gettext as _
from django.utils.translation import override as override_language
from lxml.html import builder as e

from confirmation.models import one_click_unsubscribe_link
from zerver.decorator import statsd_increment
from zerver.lib.markdown.fenced_code import FENCE_RE
from zerver.lib.message import bulk_access_messages
from zerver.lib.notification_data import get_mentioned_user_group_name
from zerver.lib.queue import queue_json_publish
from zerver.lib.send_email import FromAddress, send_future_email
from zerver.lib.soft_deactivation import soft_reactivate_if_personal_notification
from zerver.lib.topic import get_topic_resolution_and_bare_name
from zerver.lib.types import DisplayRecipientT
from zerver.lib.url_encoding import (
    huddle_narrow_url,
    personal_narrow_url,
    stream_narrow_url,
    topic_narrow_url,
)
from zerver.models import (
    Message,
    Recipient,
    Stream,
    UserMessage,
    UserProfile,
    get_context_for_message,
    get_display_recipient,
    get_user_profile_by_id,
)

if sys.version_info < (3, 9):  # nocoverage
    from backports import zoneinfo
else:  # nocoverage
    import zoneinfo

logger = logging.getLogger(__name__)


def relative_to_full_url(fragment: lxml.html.HtmlElement, base_url: str) -> None:
    # We handle narrow URLs separately because of two reasons:
    # 1: 'lxml' seems to be having an issue in dealing with URLs that begin
    # `#` due to which it doesn't add a `/` before joining the base_url to
    # the relative URL.
    # 2: We also need to update the title attribute in the narrow links which
    # is not possible with `make_links_absolute()`.
    for link_info in fragment.iterlinks():
        elem, attrib, link, pos = link_info
        match = re.match("/?#narrow/", link)
        if match is not None:
            link = re.sub(r"^/?#narrow/", base_url + "/#narrow/", link)
            elem.set(attrib, link)
            # Only manually linked narrow URLs have title attribute set.
            if elem.get("title") is not None:
                elem.set("title", link)

    # Because we were parsed with fragment_fromstring, we are
    # guaranteed there is a top-level <div>, and the original
    # top-level contents are within that.
    if len(fragment) == 1 and fragment[0].get("class") == "message_inline_image":
        # The next block handles most inline images, but for messages
        # where the entire Markdown input was just the URL of an image
        # (i.e. the entire body is a message_inline_image object), the
        # entire message body will be that image element; here, we need a
        # more drastic edit to the content.
        inner = fragment[0]
        image_link = inner.find("a").get("href")
        image_title = inner.find("a").get("title")
        title_attr = {} if image_title is None else {"title": image_title}
        inner.clear()
        inner.tag = "p"
        inner.append(e.A(image_link, href=image_link, target="_blank", **title_attr))
    else:
        # Inline images can't be displayed in the emails as the request
        # from the mail server can't be authenticated because it has no
        # user_profile object linked to it. So we scrub the inline image
        # container.
        inline_image_containers = fragment.find_class("message_inline_image")
        for container in inline_image_containers:
            container.drop_tree()

    fragment.make_links_absolute(base_url)


def fix_emojis(fragment: lxml.html.HtmlElement, base_url: str, emojiset: str) -> None:
    def make_emoji_img_elem(emoji_span_elem: lxml.html.HtmlElement) -> Dict[str, Any]:
        # Convert the emoji spans to img tags.
        classes = emoji_span_elem.get("class")
        match = re.search(r"emoji-(?P<emoji_code>\S+)", classes)
        # re.search is capable of returning None,
        # but since the parent function should only be called with a valid css element
        # we assert that it does not.
        assert match is not None
        emoji_code = match.group("emoji_code")
        emoji_name = emoji_span_elem.get("title")
        alt_code = emoji_span_elem.text
        image_url = base_url + f"/static/generated/emoji/images-{emojiset}-64/{emoji_code}.png"
        img_elem = e.IMG(alt=alt_code, src=image_url, title=emoji_name, style="height: 20px;")
        img_elem.tail = emoji_span_elem.tail
        return img_elem

    for elem in fragment.cssselect("span.emoji"):
        parent = elem.getparent()
        img_elem = make_emoji_img_elem(elem)
        parent.replace(elem, img_elem)

    for realm_emoji in fragment.cssselect(".emoji"):
        del realm_emoji.attrib["class"]
        realm_emoji.set("style", "height: 20px;")


def fix_spoilers_in_html(fragment: lxml.html.HtmlElement, language: str) -> None:
    with override_language(language):
        spoiler_title: str = _("Open Zulip to see the spoiler content")
    spoilers = fragment.find_class("spoiler-block")
    for spoiler in spoilers:
        header = spoiler.find_class("spoiler-header")[0]
        spoiler_content = spoiler.find_class("spoiler-content")[0]
        header_content = header.find("p")
        if header_content is None:
            # Create a new element to append the spoiler to)
            header_content = e.P()
            header.append(header_content)
        else:
            # Add a space.
            rear = header_content[-1] if len(header_content) else header_content
            rear.tail = (rear.tail or "") + " "
        span_elem = e.SPAN(f"({spoiler_title})", **e.CLASS("spoiler-title"), title=spoiler_title)
        header_content.append(span_elem)
        header.drop_tag()
        spoiler_content.drop_tree()


def fix_spoilers_in_text(content: str, language: str) -> str:
    with override_language(language):
        spoiler_title: str = _("Open Zulip to see the spoiler content")
    lines = content.split("\n")
    output = []
    open_fence = None
    for line in lines:
        m = FENCE_RE.match(line)
        if m:
            fence = m.group("fence")
            lang: Optional[str] = m.group("lang")
            if lang == "spoiler":
                open_fence = fence
                output.append(line)
                output.append(f"({spoiler_title})")
            elif fence == open_fence:
                open_fence = None
                output.append(line)
        elif not open_fence:
            output.append(line)
    return "\n".join(output)


def add_quote_prefix_in_text(content: str) -> str:
    """
    We add quote prefix ">" to each line of the message in plain text
    format, such that email clients render the message as quote.
    """
    lines = content.split("\n")
    output = []
    for line in lines:
        quoted_line = f"> {line}"
        output.append(quoted_line)
    return "\n".join(output)


def build_message_list(
    user: UserProfile,
    messages: List[Message],
    stream_map: Dict[int, Stream],  # only needs id, name
) -> List[Dict[str, Any]]:
    """
    Builds the message list object for the message notification email template.
    The messages are collapsed into per-recipient and per-sender blocks, like
    our web interface
    """
    messages_to_render: List[Dict[str, Any]] = []

    def sender_string(message: Message) -> str:
        if message.recipient.type in (Recipient.STREAM, Recipient.HUDDLE):
            return message.sender.full_name
        else:
            return ""

    def fix_plaintext_image_urls(content: str) -> str:
        # Replace image URLs in plaintext content of the form
        #     [image name](image url)
        # with a simple hyperlink.
        return re.sub(r"\[(\S*)\]\((\S*)\)", r"\2", content)

    def prepend_sender_to_message(
        message_plain: str, message_html: str, sender: str
    ) -> Tuple[str, str]:
        message_plain = f"{sender}:\n{message_plain}"
        message_soup = BeautifulSoup(message_html, "html.parser")
        sender_name_soup = BeautifulSoup(f"<b>{sender}</b>: ", "html.parser")
        first_tag = message_soup.find()
        if first_tag and first_tag.name == "div":
            first_tag = first_tag.find()
        if first_tag and first_tag.name == "p":
            first_tag.insert(0, sender_name_soup)
        else:
            message_soup.insert(0, sender_name_soup)
        return message_plain, str(message_soup)

    def build_message_payload(message: Message, sender: Optional[str] = None) -> Dict[str, str]:
        plain = message.content
        plain = fix_plaintext_image_urls(plain)
        # There's a small chance of colliding with non-Zulip URLs containing
        # "/user_uploads/", but we don't have much information about the
        # structure of the URL to leverage. We can't use `relative_to_full_url()`
        # function here because it uses a stricter regex which will not work for
        # plain text.
        plain = re.sub(r"/user_uploads/(\S*)", user.realm.uri + r"/user_uploads/\1", plain)
        plain = fix_spoilers_in_text(plain, user.default_language)
        plain = add_quote_prefix_in_text(plain)

        assert message.rendered_content is not None
        fragment = lxml.html.fragment_fromstring(message.rendered_content, create_parent=True)
        relative_to_full_url(fragment, user.realm.uri)
        fix_emojis(fragment, user.realm.uri, user.emojiset)
        fix_spoilers_in_html(fragment, user.default_language)
        html = lxml.html.tostring(fragment, encoding="unicode")
        if sender:
            plain, html = prepend_sender_to_message(plain, html, sender)
        return {"plain": plain, "html": html}

    def build_sender_payload(message: Message) -> Dict[str, Any]:
        sender = sender_string(message)
        return {"sender": sender, "content": [build_message_payload(message, sender)]}

    def message_header(message: Message) -> Dict[str, Any]:
        if message.recipient.type == Recipient.PERSONAL:
            grouping: Dict[str, Any] = {"user": message.sender_id}
            narrow_link = get_narrow_url(user, message)
            header = f"You and {message.sender.full_name}"
            header_html = f"<a style='color: #ffffff;' href='{narrow_link}'>{header}</a>"
        elif message.recipient.type == Recipient.HUDDLE:
            grouping = {"huddle": message.recipient_id}
            display_recipient = get_display_recipient(message.recipient)
            assert not isinstance(display_recipient, str)
            narrow_link = get_narrow_url(user, message, display_recipient=display_recipient)
            other_recipients = [r["full_name"] for r in display_recipient if r["id"] != user.id]
            header = "You and {}".format(", ".join(other_recipients))
            header_html = f"<a style='color: #ffffff;' href='{narrow_link}'>{header}</a>"
        else:
            grouping = {"stream": message.recipient_id, "topic": message.topic_name().lower()}
            stream_id = message.recipient.type_id
            stream = stream_map.get(stream_id, None)
            if stream is None:
                # Some of our callers don't populate stream_map, so
                # we just populate the stream from the database.
                stream = Stream.objects.only("id", "name").get(id=stream_id)
            narrow_link = get_narrow_url(user, message, stream=stream)
            header = f"{stream.name} > {message.topic_name()}"
            stream_link = stream_narrow_url(user.realm, stream)
            header_html = f"<a href='{stream_link}'>{stream.name}</a> > <a href='{narrow_link}'>{message.topic_name()}</a>"
        return {
            "grouping": grouping,
            "plain": header,
            "html": header_html,
            "stream_message": message.recipient.type_name() == "stream",
        }

    # # Collapse message list to
    # [
    #    {
    #       "header": {
    #                   "plain":"header",
    #                   "html":"htmlheader"
    #                 }
    #       "senders":[
    #          {
    #             "sender":"sender_name",
    #             "content":[
    #                {
    #                   "plain":"content",
    #                   "html":"htmlcontent"
    #                }
    #                {
    #                   "plain":"content",
    #                   "html":"htmlcontent"
    #                }
    #             ]
    #          }
    #       ]
    #    },
    # ]

    messages.sort(key=lambda message: message.date_sent)

    for message in messages:
        header = message_header(message)

        # If we want to collapse into the previous recipient block
        if (
            len(messages_to_render) > 0
            and messages_to_render[-1]["header"]["grouping"] == header["grouping"]
        ):
            sender = sender_string(message)
            sender_block = messages_to_render[-1]["senders"]

            # Same message sender, collapse again
            if sender_block[-1]["sender"] == sender:
                sender_block[-1]["content"].append(build_message_payload(message))
            else:
                # Start a new sender block
                sender_block.append(build_sender_payload(message))
        else:
            # New recipient and sender block
            recipient_block = {"header": header, "senders": [build_sender_payload(message)]}

            messages_to_render.append(recipient_block)

    return messages_to_render


def get_narrow_url(
    user_profile: UserProfile,
    message: Message,
    display_recipient: Optional[DisplayRecipientT] = None,
    stream: Optional[Stream] = None,
) -> str:
    """The display_recipient and stream arguments are optional.  If not
    provided, we'll compute them from the message; they exist as a
    performance optimization for cases where the caller needs those
    data too.
    """
    if message.recipient.type == Recipient.PERSONAL:
        assert stream is None
        assert display_recipient is None
        return personal_narrow_url(
            realm=user_profile.realm,
            sender=message.sender,
        )
    elif message.recipient.type == Recipient.HUDDLE:
        assert stream is None
        if display_recipient is None:
            display_recipient = get_display_recipient(message.recipient)
        assert display_recipient is not None
        assert not isinstance(display_recipient, str)
        other_user_ids = [r["id"] for r in display_recipient if r["id"] != user_profile.id]
        return huddle_narrow_url(
            realm=user_profile.realm,
            other_user_ids=other_user_ids,
        )
    else:
        assert display_recipient is None
        if stream is None:
            stream = Stream.objects.only("id", "name").get(id=message.recipient.type_id)
        return topic_narrow_url(user_profile.realm, stream, message.topic_name())


def message_content_allowed_in_missedmessage_emails(user_profile: UserProfile) -> bool:
    return (
        user_profile.realm.message_content_allowed_in_email_notifications
        and user_profile.message_content_in_email_notifications
    )


@statsd_increment("missed_message_reminders")
def do_send_missedmessage_events_reply_in_zulip(
    user_profile: UserProfile, missed_messages: List[Dict[str, Any]], message_count: int
) -> None:
    """
    Send a reminder email to a user if she's missed some PMs by being offline.

    The email will have its reply to address set to a limited used email
    address that will send a Zulip message to the correct recipient. This
    allows the user to respond to missed PMs, huddles, and @-mentions directly
    from the email.

    `user_profile` is the user to send the reminder to
    `missed_messages` is a list of dictionaries to Message objects and other data
                      for a group of messages that share a recipient (and topic)
    """
    from zerver.context_processors import common_context

    recipients = {
        (msg["message"].recipient_id, msg["message"].topic_name().lower())
        for msg in missed_messages
    }
    assert len(recipients) == 1, f"Unexpectedly multiple recipients: {recipients!r}"

    # This link is no longer a part of the email, but keeping the code in case
    # we find a clean way to add it back in the future
    unsubscribe_link = one_click_unsubscribe_link(user_profile, "missed_messages")
    context = common_context(user_profile)
    context.update(
        name=user_profile.full_name,
        message_count=message_count,
        unsubscribe_link=unsubscribe_link,
        realm_name_in_notifications=user_profile.realm_name_in_notifications,
    )

    mentioned_user_group_name = get_mentioned_user_group_name(missed_messages, user_profile)
    triggers = [message["trigger"] for message in missed_messages]
    unique_triggers = set(triggers)

    personal_mentioned = any(
        message["trigger"] == "mentioned" and message["mentioned_user_group_id"] is None
        for message in missed_messages
    )

    context.update(
        mention="mentioned" in unique_triggers or "wildcard_mentioned" in unique_triggers,
        personal_mentioned=personal_mentioned,
        wildcard_mentioned="wildcard_mentioned" in unique_triggers,
        stream_email_notify="stream_email_notify" in unique_triggers,
        mention_count=triggers.count("mentioned") + triggers.count("wildcard_mentioned"),
        mentioned_user_group_name=mentioned_user_group_name,
    )

    # If this setting (email mirroring integration) is enabled, only then
    # can users reply to email to send message to Zulip. Thus, one must
    # ensure to display warning in the template.
    if settings.EMAIL_GATEWAY_PATTERN:
        context.update(
            reply_to_zulip=True,
        )
    else:
        context.update(
            reply_to_zulip=False,
        )

    from zerver.lib.email_mirror import create_missed_message_address

    reply_to_address = create_missed_message_address(user_profile, missed_messages[0]["message"])
    if reply_to_address == FromAddress.NOREPLY:
        reply_to_name = ""
    else:
        reply_to_name = "Zulip"

    narrow_url = get_narrow_url(user_profile, missed_messages[0]["message"])
    context.update(
        narrow_url=narrow_url,
    )

    senders = list({m["message"].sender for m in missed_messages})
    if missed_messages[0]["message"].recipient.type == Recipient.HUDDLE:
        display_recipient = get_display_recipient(missed_messages[0]["message"].recipient)
        # Make sure that this is a list of strings, not a string.
        assert not isinstance(display_recipient, str)
        other_recipients = [r["full_name"] for r in display_recipient if r["id"] != user_profile.id]
        context.update(group_pm=True)
        if len(other_recipients) == 2:
            huddle_display_name = " and ".join(other_recipients)
            context.update(huddle_display_name=huddle_display_name)
        elif len(other_recipients) == 3:
            huddle_display_name = (
                f"{other_recipients[0]}, {other_recipients[1]}, and {other_recipients[2]}"
            )
            context.update(huddle_display_name=huddle_display_name)
        else:
            huddle_display_name = "{}, and {} others".format(
                ", ".join(other_recipients[:2]), len(other_recipients) - 2
            )
            context.update(huddle_display_name=huddle_display_name)
    elif missed_messages[0]["message"].recipient.type == Recipient.PERSONAL:
        context.update(private_message=True)
    elif context["mention"] or context["stream_email_notify"]:
        # Keep only the senders who actually mentioned the user
        if context["mention"]:
            senders = list(
                {
                    m["message"].sender
                    for m in missed_messages
                    if m["trigger"] == "mentioned" or m["trigger"] == "wildcard_mentioned"
                }
            )
        message = missed_messages[0]["message"]
        stream = Stream.objects.only("id", "name").get(id=message.recipient.type_id)
        topic_resolved, topic_name = get_topic_resolution_and_bare_name(message.topic_name())
        context.update(
            stream_name=stream.name,
            topic_name=topic_name,
            topic_resolved=topic_resolved,
        )
    else:
        raise AssertionError("Invalid messages!")

    # If message content is disabled, then flush all information we pass to email.
    if not message_content_allowed_in_missedmessage_emails(user_profile):
        realm = user_profile.realm
        context.update(
            reply_to_zulip=False,
            messages=[],
            sender_str="",
            realm_str=realm.name,
            huddle_display_name="",
            show_message_content=False,
            message_content_disabled_by_user=not user_profile.message_content_in_email_notifications,
            message_content_disabled_by_realm=not realm.message_content_allowed_in_email_notifications,
        )
    else:
        context.update(
            messages=build_message_list(
                user=user_profile,
                messages=[m["message"] for m in missed_messages],
                stream_map={},
            ),
            sender_str=", ".join(sender.full_name for sender in senders),
            realm_str=user_profile.realm.name,
            show_message_content=True,
        )

    # Soft reactivate the long_term_idle user personally mentioned
    soft_reactivate_if_personal_notification(
        user_profile, unique_triggers, mentioned_user_group_name
    )

    with override_language(user_profile.default_language):
        from_name: str = _("Zulip notifications")
    from_address = FromAddress.NOREPLY
    if len(senders) == 1 and settings.SEND_MISSED_MESSAGE_EMAILS_AS_USER:
        # If this setting is enabled, you can reply to the Zulip
        # message notification emails directly back to the original sender.
        # However, one must ensure the Zulip server is in the SPF
        # record for the domain, or there will be spam/deliverability
        # problems.
        #
        # Also, this setting is not really compatible with
        # EMAIL_ADDRESS_VISIBILITY_ADMINS.
        sender = senders[0]
        from_name, from_address = (sender.full_name, sender.email)
        context.update(
            reply_to_zulip=False,
        )

    email_dict = {
        "template_prefix": "zerver/emails/missed_message",
        "to_user_ids": [user_profile.id],
        "from_name": from_name,
        "from_address": from_address,
        "reply_to_email": str(Address(display_name=reply_to_name, addr_spec=reply_to_address)),
        "context": context,
    }
    queue_json_publish("email_senders", email_dict)

    user_profile.last_reminder = timezone_now()
    user_profile.save(update_fields=["last_reminder"])


def handle_missedmessage_emails(
    user_profile_id: int, missed_email_events: Iterable[Dict[str, Any]]
) -> None:
    message_ids = {
        event.get("message_id"): {
            "trigger": event.get("trigger"),
            "mentioned_user_group_id": event.get("mentioned_user_group_id"),
        }
        for event in missed_email_events
    }

    user_profile = get_user_profile_by_id(user_profile_id)
    if user_profile.is_bot:  # nocoverage
        # We don't expect to reach here for bot users. However, this code exists
        # to find and throw away any pre-existing events in the queue while
        # upgrading from versions before our notifiability logic was implemented.
        # TODO/compatibility: This block can be removed when one can no longer
        # upgrade from versions <= 4.0 to versions >= 5.0
        logger.warning("Send-email event found for bot user %s. Skipping.", user_profile_id)
        return

    if not user_profile.enable_offline_email_notifications:
        # BUG: Investigate why it's possible to get here.
        return  # nocoverage

    # Note: This query structure automatically filters out any
    # messages that were permanently deleted, since those would now be
    # in the ArchivedMessage table, not the Message table.
    messages = Message.objects.filter(
        usermessage__user_profile_id=user_profile,
        id__in=message_ids,
        usermessage__flags=~UserMessage.flags.read,
        # Cancel missed-message emails for deleted messages
    ).exclude(content="(deleted)")

    if not messages:
        return

    # We bucket messages by tuples that identify similar messages.
    # For streams it's recipient_id and topic.
    # For PMs it's recipient id and sender.
    messages_by_bucket: Dict[Tuple[int, Union[int, str]], List[Message]] = defaultdict(list)
    for msg in messages:
        if msg.recipient.type == Recipient.PERSONAL:
            # For PM's group using (recipient, sender).
            messages_by_bucket[(msg.recipient_id, msg.sender_id)].append(msg)
        else:
            messages_by_bucket[(msg.recipient_id, msg.topic_name().lower())].append(msg)

    message_count_by_bucket = {
        bucket_tup: len(msgs) for bucket_tup, msgs in messages_by_bucket.items()
    }

    for msg_list in messages_by_bucket.values():
        msg = min(msg_list, key=lambda msg: msg.date_sent)
        if msg.is_stream_message():
            context_messages = get_context_for_message(msg)
            filtered_context_messages = bulk_access_messages(user_profile, context_messages)
            msg_list.extend(filtered_context_messages)

    # Sort emails by least recently-active discussion.
    bucket_tups: List[Tuple[Tuple[int, Union[int, str]], int]] = []
    for bucket_tup, msg_list in messages_by_bucket.items():
        max_message_id = max(msg_list, key=lambda msg: msg.id).id
        bucket_tups.append((bucket_tup, max_message_id))

    bucket_tups = sorted(bucket_tups, key=lambda x: x[1])

    # Send an email per bucket.
    for bucket_tup, ignored_max_id in bucket_tups:
        unique_messages = {}
        for m in messages_by_bucket[bucket_tup]:
            message_info = message_ids.get(m.id)
            unique_messages[m.id] = dict(
                message=m,
                trigger=message_info["trigger"] if message_info else None,
                mentioned_user_group_id=message_info.get("mentioned_user_group_id")
                if message_info is not None
                else None,
            )
        do_send_missedmessage_events_reply_in_zulip(
            user_profile,
            list(unique_messages.values()),
            message_count_by_bucket[bucket_tup],
        )


def followup_day2_email_delay(user: UserProfile) -> timedelta:
    days_to_delay = 2
    user_tz = user.timezone
    if user_tz == "":
        user_tz = "UTC"
    signup_day = user.date_joined.astimezone(zoneinfo.ZoneInfo(user_tz)).isoweekday()
    if signup_day == 5:
        # If the day is Friday then delay should be till Monday
        days_to_delay = 3
    elif signup_day == 4:
        # If the day is Thursday then delay should be till Friday
        days_to_delay = 1

    # The delay should be 1 hour before the above calculated delay as
    # our goal is to maximize the chance that this email is near the top
    # of the user's inbox when the user sits down to deal with their inbox,
    # or comes in while they are dealing with their inbox.
    return timedelta(days=days_to_delay, hours=-1)


def enqueue_welcome_emails(user: UserProfile, realm_creation: bool = False) -> None:
    from zerver.context_processors import common_context

    if settings.WELCOME_EMAIL_SENDER is not None:
        # line break to avoid triggering lint rule
        from_name = settings.WELCOME_EMAIL_SENDER["name"]
        from_address = settings.WELCOME_EMAIL_SENDER["email"]
    else:
        from_name = None
        from_address = FromAddress.support_placeholder

    other_account_count = (
        UserProfile.objects.filter(delivery_email__iexact=user.delivery_email)
        .exclude(id=user.id)
        .count()
    )
    unsubscribe_link = one_click_unsubscribe_link(user, "welcome")
    context = common_context(user)
    context.update(
        unsubscribe_link=unsubscribe_link,
        keyboard_shortcuts_link=user.realm.uri + "/help/keyboard-shortcuts",
        realm_name=user.realm.name,
        realm_creation=realm_creation,
        email=user.delivery_email,
        is_realm_admin=user.is_realm_admin,
        is_demo_org=user.realm.demo_organization_scheduled_deletion_date is not None,
    )

    context["getting_organization_started_link"] = (
        user.realm.uri + "/help/getting-your-organization-started-with-zulip"
    )
    context["getting_user_started_link"] = user.realm.uri + "/help/getting-started-with-zulip"

    # Imported here to avoid import cycles.
    from zproject.backends import ZulipLDAPAuthBackend, email_belongs_to_ldap

    if email_belongs_to_ldap(user.realm, user.delivery_email):
        context["ldap"] = True
        for backend in get_backends():
            # If the user is doing authentication via LDAP, Note that
            # we exclude ZulipLDAPUserPopulator here, since that
            # isn't used for authentication.
            if isinstance(backend, ZulipLDAPAuthBackend):
                context["ldap_username"] = backend.django_to_ldap_username(user.delivery_email)
                break

    send_future_email(
        "zerver/emails/followup_day1",
        user.realm,
        to_user_ids=[user.id],
        from_name=from_name,
        from_address=from_address,
        context=context,
    )

    if other_account_count == 0:
        send_future_email(
            "zerver/emails/followup_day2",
            user.realm,
            to_user_ids=[user.id],
            from_name=from_name,
            from_address=from_address,
            context=context,
            delay=followup_day2_email_delay(user),
        )


def convert_html_to_markdown(html: str) -> str:
    # html2text is GPL licensed, so run it as a subprocess.
    markdown = subprocess.check_output(
        [os.path.join(sys.prefix, "bin", "html2text")], input=html, text=True
    ).strip()

    # We want images to get linked and inline previewed, but html2text will turn
    # them into links of the form `![](http://foo.com/image.png)`, which is
    # ugly. Run a regex over the resulting description, turning links of the
    # form `![](http://foo.com/image.png?12345)` into
    # `[image.png](http://foo.com/image.png)`.
    return re.sub("!\\[\\]\\((\\S*)/(\\S*)\\?(\\S*)\\)", "[\\2](\\1/\\2)", markdown)

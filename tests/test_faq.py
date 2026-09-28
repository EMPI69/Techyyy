"""FAQ keyword engine, cooldowns, channel rules and the auto-reply."""

from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import helpers as h
from caudal_bot.cogs import faq as faq_module
from caudal_bot.cogs.faq import FAQ_ENTRIES, PROMPT_LIFETIME_SECONDS, _compile_keyword, build_faq_embed, match_faq
from caudal_bot.common import ZWSP
from caudal_bot.views.faq import OpenTicketView, QuestionTicketButton, ShowAnswerButton

LOGINS = "🔐 Platform Logins & Credentials"
SNORKEL = "🌐 Snorkel Platform Access"
TIME_DOCTOR = "⏱️ Time Doctor Setup & Credentials"
ROLES = "🏷️ Getting Started & Discord Roles"
EXTENSION = "🔑 Caudal AI Auth Codes Extension"


def title(text: str) -> str | None:
    entry = match_faq(text)
    return entry.title if entry else None


def test_the_five_onboarding_topics_in_order():
    assert [e.title for e in FAQ_ENTRIES] == [LOGINS, SNORKEL, TIME_DOCTOR, ROLES, EXTENSION]


@pytest.mark.parametrize("text, expected", [
    # Realistic questions, one topic at a time.
    ("How do I log in?", LOGINS),
    ("I can't sign in to the platform", LOGINS),
    ("forgot my password", LOGINS),
    ("Where are my credentials?", LOGINS),
    ("Is there a signin page?", LOGINS),
    ("Where is Snorkel?", SNORKEL),
    ("link to the tasks portal please", SNORKEL),
    ("Where do I track hours?", TIME_DOCTOR),
    ("how do I install timedoctor", TIME_DOCTOR),
    ("Is TD required?", TIME_DOCTOR),
    ("do we log our work hours somewhere", TIME_DOCTOR),
    ("Can someone give me my project role?", ROLES),
    ("How do I get verified? I need to verify", ROLES),
    ("not sure how to begin", ROLES),
    ("How to get the chrome extension?", EXTENSION),
    ("it asks me for 2FA", EXTENSION),
    ("where do I find auth codes", EXTENSION),
    # Case and spacing.
    ("TIME    DOCTOR???", TIME_DOCTOR),                   # case-insensitive, any whitespace
    ("time\tdoctor login", TIME_DOCTOR),
    ("login.", LOGINS),                                    # punctuation is a word boundary
    ("2fa-code", EXTENSION),                               # so is a hyphen
])
def test_keywords_match(text, expected):
    assert title(text) == expected


@pytest.mark.parametrize("text, expected", [
    # A longer keyword beats a shorter one from another topic, whatever the list order.
    ("time doctor login not working", TIME_DOCTOR),        # not Logins' "login"
    ("snorkel login fails", SNORKEL),                      # not Logins' "login"
    ("Time Doctor password?", TIME_DOCTOR),                # 2-word "time doctor" beats "password"
    ("my verification code expired", EXTENSION),           # not Roles' "verification"
    ("need my project role", ROLES),
    ("the auth code chrome extension", EXTENSION),
    ("snorkel ai account", SNORKEL),                       # "snorkel ai" beats "account"
])
def test_most_specific_keyword_wins(text, expected):
    assert title(text) == expected


def test_equal_specificity_falls_back_to_list_order():
    # "account" (Logins, 1st) and "welcome" (Roles, 4th) are both one 7-letter word.
    assert title("welcome to my account") == LOGINS


@pytest.mark.parametrize("text", [
    "logins",           # keyword inside a longer word
    "passwords",
    "snorkeling trip",
    "stracking",
    "tdx",
    "verified",         # not "verify"
    "extensions",
    "roles",
    "",
])
def test_whole_word_boundaries_prevent_false_matches(text):
    assert title(text) is None


def test_keywords_are_regex_escaped():
    pattern = _compile_keyword("c++ (beta)")
    assert pattern.search("I use C++ (beta) daily")
    assert not pattern.search("I use c (beta)")


@pytest.mark.parametrize("entry", FAQ_ENTRIES, ids=[e.title for e in FAQ_ENTRIES])
def test_every_answer_is_quoted_and_fits_an_embed(entry):
    embed = build_faq_embed(entry, NS(mention="<@7>"))
    lines = embed.description.split("\n")
    body, blank, footnote = lines[:-2], lines[-2], lines[-1]
    assert all(line.startswith("> ") for line in body)
    assert blank == "" and footnote.startswith("-# Asked by <@7>")   # footnote stays outside the quote
    assert embed.title == entry.title                                # shown as written: no extra emoji
    assert len(embed.description) <= 4096 and len(embed.title) <= 256


def test_blank_answer_lines_keep_the_quote_bar_unbroken():
    embed = build_faq_embed(FAQ_ENTRIES[1], NS(mention="<@7>"))
    assert f"> {ZWSP}" in embed.description.split("\n")


def test_links_survive_formatting():
    text = build_faq_embed(FAQ_ENTRIES[4], NS(mention="<@7>")).description
    assert "https://chromewebstore.google.com/detail/caudal-ai-auth-codes/dieaafhpaldhiohgmdfhnjmdkgaobnbm" in text
    assert "https://experts.snorkel-ai.com/home" in build_faq_embed(FAQ_ENTRIES[1], NS(mention="<@7>")).description


# ---- cooldowns ---------------------------------------------------------------

@pytest.fixture
def faq(bot):
    return bot.faq


def test_cooldown_blocks_repeats_in_the_same_channel(faq, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(faq_module.time, "monotonic", lambda: clock[0])
    entry = FAQ_ENTRIES[0]
    assert faq._on_cooldown(1, entry) is False     # first answer goes out
    assert faq._on_cooldown(1, entry) is True      # immediate repeat is suppressed
    assert faq._on_cooldown(2, entry) is False     # other channel: independent
    assert faq._on_cooldown(1, FAQ_ENTRIES[1]) is False  # other FAQ: independent
    clock[0] += 29.9
    assert faq._on_cooldown(1, entry) is True
    clock[0] += 0.2
    assert faq._on_cooldown(1, entry) is False     # window (30s default) elapsed


def test_zero_cooldown_never_suppresses(make_config, monkeypatch):
    from caudal_bot.main import FAQBot
    faq = FAQBot(make_config(faq_cooldown=0)).faq
    assert not any(faq._on_cooldown(1, FAQ_ENTRIES[0]) for _ in range(3))


# ---- where the bot listens -----------------------------------------------------

def message(content="how do I log in?", *, author_bot=False, guild_id=h.GUILD_ID, category_id=77,
            topic=None, public=True, channel_id=55):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id, ch.topic, ch.category_id = channel_id, topic, category_id
    ch.permissions_for = lambda _role: NS(view_channel=public)
    m = MagicMock()
    m.content, m.channel = content, ch
    m.author = NS(bot=author_bot, id=h.OUTSIDER_ID, mention=f"<@{h.OUTSIDER_ID}>")
    m.guild = NS(id=guild_id, default_role=object())
    m.reply = AsyncMock()
    return m


@pytest.mark.parametrize("kwargs, listens", [
    ({}, True),
    ({"author_bot": True}, False),
    ({"guild_id": 42}, True),                  # every server the bot is in, set up or not
    ({"category_id": h.TICKETS_CAT}, False),   # never inside the tickets category
    ({"public": False}, False),                # private channels need an allow-list
])
def test_should_listen(faq, kwargs, listens):
    assert faq._should_listen(message(**kwargs)) is listens


def test_allow_list_overrides_public_check(make_config):
    from caudal_bot.main import FAQBot
    faq = FAQBot(make_config(faq_channel_ids=frozenset({55}))).faq
    assert faq._should_listen(message(public=False, channel_id=55))
    assert not faq._should_listen(message(public=True, channel_id=56))


# ---- private answers ------------------------------------------------------------

async def test_keyword_gets_only_a_short_public_prompt(faq):
    m = message("Where do I track hours?")
    await faq.on_message(m)
    args, kw = m.reply.call_args
    assert args == (f"📘 **{TIME_DOCTOR}**: tap **Show answer** to read it privately.",)
    assert "embed" not in kw                                    # the answer itself is never public
    assert [c.custom_id for c in kw["view"].children] == ["faq:show:2"]
    assert kw["delete_after"] == PROMPT_LIFETIME_SECONDS == 60  # and the prompt cleans itself up
    assert kw["mention_author"] is False and kw["allowed_mentions"].users is False


def clicked(prompt_reference=None, channel_id=55):
    prompt = NS(reference=NS(message_id=prompt_reference) if prompt_reference else None)
    i = h.interaction(who=h.OUTSIDER, message=prompt)
    i.channel = NS(id=channel_id)
    return i


async def test_show_answer_is_ephemeral_and_remembers_the_question(faq):
    i = clicked(prompt_reference=4242)
    await faq.show_answer(i, 2)
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True                              # only the person who clicked sees it
    assert kw["embed"].title == TIME_DOCTOR and "Asked by" not in kw["embed"].description
    assert [c.custom_id for c in kw["view"].children] == ["faq:ticket:55:4242"]
    assert kw["view"].children[0].item.label == "Not helpful? Open a Ticket"


async def test_show_answer_without_a_question_uses_the_plain_ticket_button(faq):
    i = clicked(prompt_reference=None)
    await faq.show_answer(i, 0)
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True and isinstance(kw["view"], OpenTicketView)
    assert kw["view"].timeout is not None                       # not kept in memory forever


async def test_a_stale_show_answer_button_is_answered_politely(faq):
    i = clicked()
    await faq.show_answer(i, 99)                                 # entry removed since the prompt was sent
    assert "no longer available" in h.last_text(i.response.send_message)
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_buttons_survive_a_restart_via_their_custom_ids(bot):
    """After a restart only the custom_id is left; the DynamicItems rebuild the button from it."""
    show = await ShowAnswerButton.from_custom_id(None, None, {"index": "3"})
    assert show.index == 3 and show.item.custom_id == "faq:show:3"
    ticket = await QuestionTicketButton.from_custom_id(None, None, {"channel": "55", "message": "4242"})
    assert ticket.source == (55, 4242)
    i = h.interaction(who=h.OUTSIDER)
    i.client = NS(faq=NS(show_answer=AsyncMock()), tickets=NS(open_ticket=AsyncMock()))
    await show.callback(i)
    i.client.faq.show_answer.assert_awaited_once_with(i, 3)
    await ticket.callback(i)
    i.client.tickets.open_ticket.assert_awaited_once_with(i, question=(55, 4242))


async def test_ticket_from_a_private_answer_still_quotes_the_question(bot):
    question = h.msg(h.OWNER, h.now(), "how do I log in?", mid=4242)
    channel = h.text_channel(55, messages=[question])
    i = h.interaction(who=h.OWNER)
    i.guild = NS(get_channel=lambda cid: channel if cid == 55 else None)
    assert await bot.tickets._original_question(i, (55, 4242)) == "how do I log in?"
    assert await bot.tickets._original_question(i, (55, 1)) is None       # deleted meanwhile
    assert await bot.tickets._original_question(i, (99, 4242)) is None    # not a channel here


# ---- /faq -----------------------------------------------------------------------------

@pytest.mark.parametrize("query, expected", [
    ("where do I track hours?", TIME_DOCTOR),                   # keyword matching
    (EXTENSION, EXTENSION),                                     # what autocomplete fills in
    ("🌐 snorkel platform access", SNORKEL),                    # titles ignore case
])
async def test_faq_command_answers_privately(faq, query, expected):
    i = h.interaction(who=h.OUTSIDER)
    await faq.answer_query(i, query)
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True and kw["embed"].title == expected
    assert [c.custom_id for c in kw["view"].children] == ["faq:open_ticket"]


@pytest.mark.parametrize("query", [None, "what's for lunch?"])
async def test_faq_command_lists_topics_when_nothing_matches(faq, query):
    i = h.interaction(who=h.OUTSIDER)
    await faq.answer_query(i, query)
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True and kw["embed"].title == "📘 FAQ topics"
    assert all(e.title in kw["embed"].description for e in FAQ_ENTRIES)
    assert ("No FAQ matched **what's for lunch?**" in kw["embed"].description) is bool(query)


@pytest.mark.parametrize("typed, expected", [
    ("", [LOGINS, SNORKEL, TIME_DOCTOR, ROLES, EXTENSION]),
    ("time", [TIME_DOCTOR]),
    ("2fa", [EXTENSION]),                                       # keywords count too
    ("CHROME", [EXTENSION]),
    ("zzz", []),
])
def test_faq_autocomplete(faq, typed, expected):
    assert [c.value for c in faq.suggest(typed)] == expected


@pytest.mark.parametrize("topic", ["ticket-owner:3", "ticket-owner:3 | delete_at:99"])
async def test_ticket_channels_are_left_to_the_tickets_cog(faq, topic):
    # Even outside the tickets category (e.g. an archived ticket in the archive category).
    m = message("login", topic=topic, category_id=h.ARCHIVE_CAT)
    await faq.on_message(m)
    m.reply.assert_not_awaited()


async def test_no_keyword_no_reply(faq):
    m = message("hello there")
    await faq.on_message(m)
    m.reply.assert_not_awaited()


async def test_missing_reply_permission_is_logged_not_raised(faq, caplog):
    m = message("login")
    m.reply = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    await faq.on_message(m)
    assert "Missing permission to reply" in caplog.text

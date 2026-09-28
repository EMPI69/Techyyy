"""Transcript rendering and sanitisation, message grouping, local storage and the 14-day
purge, and delivery to the log channel and the owner's DM."""

from __future__ import annotations

import datetime as dt
import html.parser
import os
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import helpers as h
from caudal_bot.common import _code_block, build_archive_embed
from caudal_bot.transcripts import cleanup
from caudal_bot.transcripts.generator import (
    _Resolver, build_html_transcript, build_transcript, render_markdown,
)

T0 = dt.datetime(2026, 9, 25, 10, 0, tzinfo=dt.timezone.utc)
R = _Resolver(users={2: "some_user_name"}, roles={9: "Staff"}, channels={10: "ticket-bob"})


# ---- markdown -----------------------------------------------------------------------

@pytest.mark.parametrize("source, rendered", [
    ("**b** *i* __u__ ~~s~~ ||sp||",
     '<strong>b</strong> <em>i</em> <u>u</u> <s>s</s> <span class="spoiler">sp</span>'),
    ("`**not bold**`", "<code>**not bold**</code>"),
    ("> quoted\n> two", "<blockquote>quoted<br>two</blockquote>"),
    ("-# small", '<div class="subtext">small</div>'),
    ("# Big", "<h3>Big</h3>"),
    ("<@2> <@&9> <#10>", '<span class="mention">@some_user_name</span> <span class="mention">@Staff</span> '
                         '<span class="mention">#ticket-bob</span>'),   # underscores in names aren't italicised
    ("<@404>", '<span class="mention">@unknown-user</span>'),
    ("<:party:123>", ":party:"),
    ("[docs](https://x.io/d)", '<a href="https://x.io/d" rel="noopener noreferrer">docs</a>'),
    ("see https://example.com/a?b=1&c=2.",
     'see <a href="https://example.com/a?b=1&amp;c=2" rel="noopener noreferrer">https://example.com/a?b=1&amp;c=2</a>.'),
])
def test_markdown(source, rendered):
    assert render_markdown(source, R) == rendered


def test_code_blocks_are_escaped_and_not_formatted():
    out = render_markdown("```py\nx = <b>**1**</b>\n```", R)
    assert out == "<pre><code>x = &lt;b&gt;**1**&lt;/b&gt;</code></pre>"


def test_timestamp_tokens_render_as_utc_dates():
    assert render_markdown("<t:1790330400:D>", R) == "<code>25 September 2026 UTC</code>"
    assert "<t:" not in render_markdown("<t:99999999999999999999:R>", R)  # absurd values don't crash


# ---- XSS / injection -------------------------------------------------------------------

class TagAudit(html.parser.HTMLParser):
    """Records anything executable that survives in the final markup."""

    def __init__(self):
        super().__init__()
        self.bad: list[tuple] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "iframe", "object", "embed", "form", "base", "meta") and not (
                tag == "meta" and dict(attrs).get("charset") or dict(attrs).get("name") in ("viewport", "referrer")):
            self.bad.append((tag,))
        for key, value in attrs:
            value = (value or "").strip().lower()
            if key.startswith("on") or (key in ("href", "src") and value.startswith(("javascript:", "data:text", "vbscript:"))):
                self.bad.append((tag, key, value))


PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "[click](javascript:alert(1))",
    "<a href=javascript:alert(1)>x</a>",
    "```</code></pre><script>alert(1)</script>```",
    "**<svg onload=alert(1)>**",
    "<@1><iframe src=//evil>",
    "`<img src=x onerror=1>`",
    "> <script>q</script>",
    "https://ok.io/\"onmouseover=\"alert(1)",
    "<base href=//evil/>",
]


def audit(page: str) -> list:
    parser = TagAudit()
    parser.feed(page)
    return parser.bad


@pytest.mark.parametrize("payload", PAYLOADS)
def test_message_content_cannot_inject_markup(payload):
    author = h.user(5, "eve")
    page = build_html_transcript(guild_name="G", channel_name="t", meta=[], messages=[h.msg(author, T0, payload)],
                                 resolver=R)
    assert audit(page) == []


def test_every_user_controlled_field_is_escaped():
    evil = NS(id=666, name="<b>evil</b>", display_name='"><img src=x onerror=alert(1)>', bot=False,
              display_avatar=NS(url='https://cdn/"onload="alert(1)'))
    embed = discord.Embed(title="<script>t</script>", description="<img src=x onerror=1>", url="javascript:alert(1)")
    embed.add_field(name="<b onmouseover=1>", value="`</code><script>`")
    embed.set_footer(text="<script>f</script>")
    embed.set_author(name="<script>a</script>")
    att = NS(filename='"><script>.png', url='https://cdn/x"onerror="1', content_type="image/png", size=10)
    page = build_html_transcript(guild_name="<G>", channel_name="x<y", meta=[("<k>", "<v onclick=1>")],
                                 messages=[h.msg(evil, T0, "", embeds=[embed], attachments=[att])], resolver=R)
    assert audit(page) == []
    assert "&lt;script&gt;" in page and 'href="javascript:' not in page


# ---- rendering & grouping -------------------------------------------------------------

def realistic_page():
    bob = h.user(1, "bob")
    welcome = h.welcome(at=T0, question="help me")
    file_att = NS(filename="log.txt", url="https://cdn.discordapp.com/log.txt", content_type="text/plain", size=2048)
    image = NS(filename="shot.png", url="https://cdn.discordapp.com/shot.png", content_type="image/png", size=10)
    messages = [welcome,
                h.msg(bob, T0 + dt.timedelta(minutes=1), "hi **there**"),
                h.msg(bob, T0 + dt.timedelta(minutes=2), "second line", attachments=[image]),
                h.msg(bob, T0 + dt.timedelta(minutes=20), "later", attachments=[file_att])]
    return build_html_transcript(guild_name="Caudal", channel_name="ticket-bob",
                                 meta=[("Opener", "bob (1)"), ("Resolution", "1h 3m")], messages=messages,
                                 resolver=_Resolver(users={1: "Bob"}, roles={9: "Staff"}), generated_by="SupportBot")


def test_consecutive_messages_are_grouped_like_discord():
    page = realistic_page()
    # bot card, bob, bob-after-18-min; bob's 1-minute follow-up joins his previous group
    assert page.count('class="msg first"') == 3
    assert page.count('class="hover-time"') == 1


def test_page_has_header_badges_embeds_and_attachments():
    page = realistic_page()
    assert "<title>Transcript • #ticket-bob • Caudal</title>" in page
    assert "<dt>Resolution</dt><dd>1h 3m</dd>" in page
    assert page.count('<span class="badge">APP</span>') == 1          # only the bot
    assert "border-left-color:#2ecc71" in page and "📊 Ticket Status" in page
    assert 'title="2026-09-25 10:01:00 UTC"' in page                    # exact time on hover
    assert '<img class="att-img" src="https://cdn.discordapp.com/shot.png"' in page
    assert "2.0 KB" in page and "Generated by SupportBot • 4 message(s)" in page
    assert '<meta name="referrer" content="no-referrer">' in page


def test_text_transcript_format():
    msgs = [h.msg(h.user(1, "bob"), T0 + dt.timedelta(minutes=1), "hi\nsecond line"),
            h.msg(h.user(2, "mod"), T0 + dt.timedelta(minutes=2), "")]
    text = build_transcript(channel_name="ticket-bob", opener="bob (1)", claimed_staff="mod (2)", closed_by="mod (2)",
                            reason="Fixed", opened_at=T0, closed_at=T0 + dt.timedelta(hours=1, minutes=3),
                            messages=msgs, truncated=True, response_time="4m 12s")
    assert "Ticket Transcript — #ticket-bob" in text and "Resolution    : 1h 3m (opened → closed)" in text
    assert "Response Time : 4m 12s" in text and "Rating" not in text
    assert "Deleted By    : — (archived, not yet deleted)" in text
    assert "Messages      : 2 (only the first 2 were captured)" in text
    assert "[2026-09-25 10:01] bob: hi\n    second line" in text and "mod: [no text content]" in text


@pytest.mark.parametrize("text", ["a" * 5000, "```" * 600, "x​``​`y " * 300, "`" * 7, "hi"])
def test_code_block_never_breaks_or_exceeds_the_field_limit(text):
    block = _code_block(text)
    assert len(block) <= 1000                 # Discord embed fields cap at 1024
    assert block.count("```") == 2            # user backticks can't close the block early


# ---- storage & 14-day purge --------------------------------------------------------------

def test_saving_replaces_the_previous_name_atomically(tmp_path):
    cleanup.save_html_transcript(tmp_path, 100, "closed-bob", "old")
    cleanup.save_html_transcript(tmp_path, 100, "Ticket-Bob!", "new")   # renamed on re-open, sanitised
    files = [p.name for p in tmp_path.iterdir()]
    assert files == ["100__ticket-bob.html"] and (tmp_path / files[0]).read_text() == "new"
    assert cleanup.find_transcript(tmp_path, 100).name == "100__ticket-bob.html"
    assert cleanup.find_transcript(tmp_path, 999) is None


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    now = time.time()

    def make(name, age_days=0.0, *, directory=False):
        p = d / name
        p.mkdir() if directory else p.write_text("x")
        os.utime(p, (now - age_days * 86400,) * 2)
        return p
    return NS(dir=d, now=now, make=make)


def test_purge_removes_only_old_transcripts(folder):
    old = [folder.make("1__a.html", 15), folder.make("2__b.txt", 20), folder.make("3__c.HTML", 30)]
    kept = [folder.make("4__fresh.html", 1), folder.make("5__edge.html", 13.9), folder.make("notes.md", 60),
            folder.make(".gitkeep", 60), folder.make("sub", 60, directory=True)]
    removed = cleanup.purge_old_transcripts(folder.dir, now=folder.now)
    assert sorted(p.name for p in removed) == sorted(p.name for p in old)
    assert all(p.exists() for p in kept) and not any(p.exists() for p in old)


def test_purge_leaves_in_progress_writes_alone(folder):
    live = folder.make("6__t.html.partial", 0.01)       # being written right now
    stale = folder.make("7__t.html.partial", 1)         # left behind by a crash
    removed = cleanup.purge_old_transcripts(folder.dir, now=folder.now)
    assert [p.name for p in removed] == [stale.name] and live.exists()


def test_purge_isolates_per_file_errors(folder, monkeypatch):
    locked, other = folder.make("8__locked.html", 40), folder.make("9__old.html", 40)
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self.name == locked.name:
            raise PermissionError("in use")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    removed = cleanup.purge_old_transcripts(folder.dir, now=folder.now)
    assert [p.name for p in removed] == [other.name] and locked.exists()


def test_purge_of_a_missing_folder_is_a_no_op(tmp_path):
    assert cleanup.purge_old_transcripts(tmp_path / "nope") == []


async def test_purge_loop_uses_the_configured_folder(bot, folder, monkeypatch):
    monkeypatch.setattr(type(bot.config), "transcripts_dir", property(lambda _self: folder.dir))
    folder.make("1__old.html", 30)
    await bot.transcript_cleanup._purge_loop()   # one iteration
    assert list(folder.dir.iterdir()) == []


# ---- delivery ----------------------------------------------------------------------------

def closed_ticket(bot):
    created = h.ago(hours=2)
    card = h.msg(h.ME, created + dt.timedelta(minutes=90), embeds=[build_archive_embed(h.STAFF, 0, "Fixed")], mid=777)
    return h.text_channel(created_at=created, messages=[
        h.welcome(f"🟡 In Progress (Claimed by <@{h.STAFF_ID}>)", created),
        h.msg(h.OWNER, created + dt.timedelta(minutes=1), "help"),
        h.msg(h.STAFF, created + dt.timedelta(minutes=5, seconds=12), "on it"),
        card])


async def test_gather_computes_metrics_from_one_history_read(bot):
    data = await bot.transcripts.gather(closed_ticket(bot), deleted_by=None, deleted=True)
    assert data.response_time == "5m 12s" and data.duration == "1h 30m"  # staff replied at +5:12
    assert data.handled_by_id == h.STAFF_ID and data.closer_id == h.STAFF_ID and data.reason == "Fixed"
    assert data.html.startswith("<!DOCTYPE html>") and "Close Reason  : Fixed" in data.text
    assert "Rating" not in data.text and "Rating" not in data.html


async def test_legacy_archive_card_with_a_rating_field_still_parses(bot):
    """Cards posted before the survey was removed may carry a rating field; it's ignored."""
    ch = closed_ticket(bot)
    ch.msgs[-1].embeds[0].add_field(name="⭐ User Rating", value="⭐⭐⭐⭐ 4/5")
    data = await bot.transcripts.gather(ch, deleted_by=None, deleted=True)
    assert (data.closer_id, data.reason) == (h.STAFF_ID, "Fixed")
    assert "User Rating" not in data.text.split("=" * 60)[2]      # not in the header


async def test_log_channel_gets_summary_with_html_and_txt(bot):
    lg = h.log_channel()
    bot.transcript_channel = lambda _guild: lg
    ch = closed_ticket(bot)
    await bot.transcripts.post_to_log(ch, h.STAFF, await bot.transcripts.gather(ch, deleted_by=h.STAFF, deleted=True))
    kw = lg.posted[-1].kwargs
    assert [f.filename for f in kw["files"]] == [f"transcript-{ch.name}.txt"]      # exactly one: the .txt
    assert b"Ticket Transcript" in kw["files"][0].fp.read()
    fields = {f.name: f.value for f in kw["embed"].fields}
    assert set(fields) >= {"👤 Owner", "🛡️ Handled By", "🔒 Closed By", "⚡ Response Time", "⏱️ Total Resolution Time"}
    assert fields["⏱️ Total Resolution Time"] == "1h 30m" and fields["🔒 Closed By"] == f"<@{h.STAFF_ID}>"
    assert fields["⚡ Response Time"] == "5m 12s" and fields["🛡️ Handled By"] == f"<@{h.STAFF_ID}>"
    assert not any("Rating" in name for name in fields)
    assert kw["allowed_mentions"].users is False   # the summary never pings anyone


async def test_log_summary_without_attach_permission_still_posts(bot):
    lg = h.log_channel(attach=False)
    bot.transcript_channel = lambda _guild: lg
    ch = closed_ticket(bot)
    await bot.transcripts.post_to_log(ch, None, await bot.transcripts.gather(ch, deleted_by=None, deleted=True))
    kw = lg.posted[-1].kwargs
    assert "files" not in kw and "Attach Files" in kw["embed"].footer.text


async def test_owner_dm_is_the_summary_and_both_files_only(bot):
    h.OWNER.send = AsyncMock(return_value=NS(id=4242))
    try:
        ch = closed_ticket(bot)
        data = await bot.transcripts.gather(ch, deleted_by=None, deleted=False)
        await bot.transcripts.dm_owner(ch, data, h.STAFF, "Fixed", 9999999999)
        kw = h.OWNER.send.call_args.kwargs
        assert set(kw) == {"embed"}                                   # no files, no survey, no buttons
        embed = kw["embed"]
        assert embed.title == "🔒 Your ticket was closed" and embed.color == discord.Color(0xED4245)
        assert f"**#{ch.name}**" in embed.description and f"**{ch.guild.name}**" in embed.description
        assert "attached" not in embed.description
        fields = {f.name: f.value for f in embed.fields}
        assert fields["🔒 Closed By"] == h.STAFF.name and fields["📝 Reason"] == "> Fixed"
    finally:
        del h.OWNER.send


async def test_closed_dms_are_ignored_quietly(bot):
    h.OWNER.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Cannot send messages"))
    try:
        ch = closed_ticket(bot)
        data = await bot.transcripts.gather(ch, deleted_by=None, deleted=False)
        await bot.transcripts.dm_owner(ch, data, h.STAFF, "x", 1)   # must not raise
    finally:
        del h.OWNER.send


async def test_store_writes_the_html_for_the_dashboard(bot):
    ch = closed_ticket(bot)
    await bot.transcripts.store(ch, await bot.transcripts.gather(ch, deleted_by=None, deleted=False))
    saved = cleanup.find_transcript(bot.config.transcripts_dir, ch.id)
    assert saved is not None and saved.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

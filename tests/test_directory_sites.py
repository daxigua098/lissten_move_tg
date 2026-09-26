"""目录站适配层：URL 构造、字段归一与两份页面的解析（F-R20 / F-R21）。"""

from __future__ import annotations

import json

import pytest

from app.core.directory_sites import (
    COMBOT,
    SCOPE_CHANNELS,
    SCOPE_GLOBAL,
    TGME,
    combot_page_url,
    marker_from_href,
    normalize_language,
    pages_total,
    parse_combot_page,
    parse_page,
    parse_tgme_listing,
    restore_tgme_marker,
    tgme_page_url,
)
from app.core.errors import ValidationFailedError

# combot 真实返回的字段形态（``i`` 是 base64 头像，解析时必须丢掉）
COMBOT_PAYLOAD = json.dumps(
    [
        {
            "t": "搜群神器|中文频道|中文导航群",
            "u": "qqpp",
            "s": 172156,
            "pc": "none",
            "l": "ZH",
            "a": "",
            "i": "LONG_BASE64_AVATAR",
            "p": 1,
            "c": -1001764441693,
            "b": 0,
        },
        {
            "t": "成都本地车主聚集地",
            "u": "rongcheng_travel",
            "s": 13797,
            "pc": "none",
            "l": "N/A",
            "a": "",
            "i": "LONG_BASE64_AVATAR",
            "p": 2,
            "c": -1002150667587,
            "b": 0,
        },
    ],
    ensure_ascii=False,
)

# tg-me 列表页真实结构：分类名带 ``+`` 与 ``&amp;``，条目标识是 ``com.`` 开头
TGME_HTML = """
<ul>
  <li class="row clear list">
  <a href="https://www.tg-me.com/Money+&amp;+Crypto+News/com.Wealth">
  <div class="right btn join">Group</div> </a>
  <div class="text">
  <img src="https://img.tg-me.com/icon/We/Wealth.jpg" onerror="this.src='/img/none.jpg';"/>
  <div class="name">
  <h2><a href="https://www.tg-me.com/Money+&amp;+Crypto+News/com.Wealth">
  Wealth &amp; Crypto</a></h2>
  <div class="left">
  <div class="time"> 5,205,136 Members
  (<time datetime="2026-09-26 08:30:44">2026-09-26</time>) </div>
  <div class="time">
  <a href="https://www.tg-me.com/Money+&amp;+Crypto+News/com.Wealth">com.Wealth</a>
  </div>
  </div> </div> </div> </li>
  <li class="row clear list">
  <a href="https://www.tg-me.com/ANGEL+Mr-Buzz/com.joinchat-qu9ID0yVWhtiYmQ0">
  <div class="right btn join">Channel</div> </a>
  <div class="text">
  <img src="https://img.tg-me.com/icon/qu/qu9ID0yVWhtiYmQ0.jpg"/>
  <div class="name">
  <h2><a href="https://www.tg-me.com/ANGEL+Mr-Buzz/com.joinchat-qu9ID0yVWhtiYmQ0">
  ANGEL Mr. Buzz</a></h2>
  <div class="left">
  <div class="time"> 4,927,334 Members
  (<time datetime="2026-09-26 08:30:44">2026-09-26</time>) </div>
  </div> </div> </div> </li>
</ul>
"""


def test_normalize_language_maps_to_project_codes() -> None:
    assert normalize_language("ZH") == "zh"
    assert normalize_language("TR") == "tr"
    assert normalize_language("pt-BR") == "pt"
    # 认不出来的一律 None，不要往库里塞脏值
    assert normalize_language("N/A") is None
    assert normalize_language("") is None
    assert normalize_language(None) is None
    assert normalize_language("123") is None
    assert normalize_language("中文") is None


def test_restore_tgme_marker_handles_both_shapes() -> None:
    # com. 是 @ 的 SEO 替换
    assert restore_tgme_marker("com.Wealth") == ("Wealth", None)
    assert restore_tgme_marker("com.qqpp") == ("qqpp", None)
    # joinchat- 对应邀请链接
    assert restore_tgme_marker("com.joinchat-qu9ID0yVWhtiYmQ0") == (
        None,
        "https://t.me/+qu9ID0yVWhtiYmQ0",
    )
    # 认不出来就都不给，宁可少存也不存错
    assert restore_tgme_marker("not-a-marker") == (None, None)
    assert restore_tgme_marker("com.") == (None, None)
    assert restore_tgme_marker("com.带中文") == (None, None)


def test_marker_from_href_skips_category_segment() -> None:
    href = "https://www.tg-me.com/Money+&amp;+Crypto+News/com.Wealth"

    assert marker_from_href(href) == "com.Wealth"
    assert marker_from_href("https://www.tg-me.com/telegram-group/crypto/2.html") is None
    assert marker_from_href("") is None


def test_combot_page_url_uses_path_style_language() -> None:
    # 实测 ?lang= 无效，语言必须走路径
    assert combot_page_url("zh", 0) == (
        "https://combot.org/api/chart/zh?limit=100&offset=0&only_avatars=false"
    )
    assert "/api/chart/all?" in combot_page_url(SCOPE_GLOBAL, 100)
    assert "/api/chart/channels/all?" in combot_page_url(SCOPE_CHANNELS, 0)
    assert combot_page_url("zh", 200).endswith("offset=200&only_avatars=false")


def test_tgme_page_url_and_validation() -> None:
    assert tgme_page_url("车友", 2) == "https://www.tg-me.com/telegram-group/车友/2.html"
    # 页码越界钳到 1
    assert tgme_page_url("crypto", 0).endswith("/telegram-group/crypto/1.html")
    with pytest.raises(ValidationFailedError):
        tgme_page_url("", 1)


def test_pages_total_uses_measured_catalog_sizes() -> None:
    assert pages_total(COMBOT, SCOPE_GLOBAL) == 381
    assert pages_total(COMBOT, SCOPE_CHANNELS) == 198
    assert pages_total(COMBOT, "zh") == 24
    # 没实测过的语言与 tg-me 不算，界面显示"未知"
    assert pages_total(COMBOT, "tr") is None
    assert pages_total(TGME, "车友") is None


def test_parse_combot_page_drops_avatar_and_keeps_ids() -> None:
    entries = parse_combot_page(COMBOT_PAYLOAD, scope="zh")

    assert len(entries) == 2
    first = entries[0]
    assert first.source_site == COMBOT
    assert first.title == "搜群神器|中文频道|中文导航群"
    assert first.username == "qqpp"
    assert first.tg_id == -1001764441693
    assert first.member_count == 172156
    assert first.language == "zh"
    assert first.rank == 1
    assert first.chat_type == "supergroup"
    assert first.source_url == "https://t.me/qqpp"
    # N/A 的语言归一成 None
    assert entries[1].language is None
    # 频道榜的条目类型跟着范围走
    assert parse_combot_page(COMBOT_PAYLOAD, scope=SCOPE_CHANNELS)[0].chat_type == "channel"


def test_parse_combot_page_tolerates_wrappers_and_junk() -> None:
    wrapped = json.dumps({"items": [{"t": "群", "u": "g1", "c": -100111, "s": "1,234"}]})

    entries = parse_combot_page(wrapped)
    assert len(entries) == 1
    # 字符串形态的成员数也要能读
    assert entries[0].member_count == 1234
    # 既没有标题也没有用户名的条目丢掉
    assert parse_combot_page(json.dumps([{"i": "x"}])) == []

    with pytest.raises(ValidationFailedError):
        parse_combot_page("not-json")


def test_parse_tgme_listing_restores_username_and_invite() -> None:
    entries = parse_tgme_listing(TGME_HTML)

    assert len(entries) == 2
    first, second = entries
    assert first.source_site == TGME
    # HTML 实体要还原
    assert first.title == "Wealth & Crypto"
    assert first.username == "Wealth"
    assert first.invite_link is None
    assert first.member_count == 5205136
    assert first.chat_type == "group"
    assert second.chat_type == "channel"
    assert second.username is None
    assert second.invite_link == "https://t.me/+qu9ID0yVWhtiYmQ0"
    assert second.source_url == "https://t.me/+qu9ID0yVWhtiYmQ0"
    # 分类名（Money & Crypto News）绝不能被当成用户名
    assert all("Money" not in (entry.username or "") for entry in entries)
    # tg-me 不给数字 ID
    assert all(entry.tg_id is None for entry in entries)


def test_parse_tgme_listing_handles_empty_page() -> None:
    assert parse_tgme_listing("") == []
    assert parse_tgme_listing("<html><body>no items</body></html>") == []


def test_parse_page_dispatches_by_site() -> None:
    assert len(parse_page(COMBOT, COMBOT_PAYLOAD)) == 2
    assert len(parse_page(TGME, TGME_HTML)) == 2
    with pytest.raises(ValidationFailedError):
        parse_page("telegram", "{}")

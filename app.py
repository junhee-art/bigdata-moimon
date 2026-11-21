from __future__ import annotations

import csv
import html
import io
import re
from collections import Counter
from pathlib import Path

import streamlit as st

from chatbot import (
    DEFAULT_MODEL,
    ChatError,
    build_messages,
    canned_reply,
    direct_reply,
    list_models,
    stream_chat,
    trim_repetition,
)
from schedule_parser import WEEKDAYS, parse_schedule_text

# pandas/pyarrow 없이 동작합니다(DLL이 막힌 PC 대응). 표는 HTML, 차트는 글자 막대로 그립니다.

DEFAULT_CSV = Path(__file__).parent / "data" / "충청북도 옥천군_대표홈페이지 평생교육동아리 목록_20260814.csv"
EXPECTED_COLUMNS = ["구분", "동아리명", "결성일자", "모임주기", "모임시간", "동아리설명", "데이터기준일자"]
PARSED_COLUMNS = ["정규화 주기", "주차", "요일", "시작 시각", "시간대", "일정 상태", "검토 메모"]
REVIEW_FIELDS = ["결성일자", "모임주기", "모임시간", "동아리설명"]
STATUS_COLORS = {
    "일정 정보 구조화": "green",
    "일부 정보 추출": "orange",
    "검토 필요": "red",
    "정보 없음": "gray",
}
PAGE_SIZE = 20


st.set_page_config(
    page_title="모임ON | 옥천 동아리 일정 탐색기",
    page_icon=":material/event:",
    layout="wide",
)


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def md_escape(value: object) -> str:
    """원문에 들어 있는 마크다운·수식 기호가 서식으로 해석되지 않게 막습니다."""
    return re.sub(r"([\\`*_{}\[\]<>$~#|])", r"\\\1", clean_text(value))


@st.cache_data(show_spinner=False)
def read_csv_bytes(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [{column: clean_text(row.get(column)) for column in EXPECTED_COLUMNS} for row in reader]


def build_rows(source: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{**row, **parse_schedule_text(row["모임주기"], row["모임시간"])} for row in source]


TABLE_CSS = """
<style>
.moimon-table { overflow-x: auto; margin-bottom: 0.5rem; }
.moimon-table table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
.moimon-table th, .moimon-table td {
    border-bottom: 1px solid rgba(128, 128, 128, 0.25);
    padding: 0.5rem 0.75rem;
    text-align: left;
    vertical-align: top;
    word-break: keep-all;
}
.moimon-table th { white-space: nowrap; font-weight: 600; background: rgba(128, 128, 128, 0.10); }
.moimon-table tbody tr:hover { background: rgba(128, 128, 128, 0.07); }
</style>
"""
st.html(TABLE_CSS)


def show_table(rows: list[dict[str, str]], columns: list[str]) -> None:
    """st.dataframe 대신 쓰는 HTML 표 (pyarrow 불필요, 가로 스크롤 지원)."""
    head = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(clean_text(row.get(column, '')))}</td>" for column in columns) + "</tr>"
        for row in rows
    )
    st.html(f'<div class="moimon-table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def show_bars(pairs: list[tuple[str, int]], label: str) -> None:
    """st.bar_chart 대신 쓰는 글자 막대 표."""
    peak = max(count for _, count in pairs)
    bars = [{label: name, "행 수": str(count), "막대": "█" * max(1, round(count / peak * 20))} for name, count in pairs]
    show_table(bars, [label, "행 수", "막대"])


def pager_state(key: str, signature: object, total_items: int, page_size: int = PAGE_SIZE) -> tuple[int, int]:
    """(현재 페이지, 전체 페이지). 검색·필터 조건(signature)이 바뀌면 1페이지로 돌아갑니다."""
    total_pages = max(1, (total_items - 1) // page_size + 1)
    if st.session_state.get(f"{key}_sig") != signature:
        st.session_state[f"{key}_sig"] = signature
        st.session_state[key] = 1
    page = min(max(int(st.session_state.get(key, 1)), 1), total_pages)
    st.session_state[key] = page
    return page, total_pages


def _go_to_page(key: str, page: int) -> None:
    st.session_state[key] = page


def show_pager(key: str, page: int, total_pages: int) -> None:
    """처음 · 이전 · 현재 위치 · 다음 · 마지막 버튼. 페이지가 하나뿐이면 아무것도 그리지 않습니다."""
    if total_pages <= 1:
        return
    first, prev, middle, nxt, last = st.columns([1, 1, 2, 1, 1])
    first.button("« 처음", key=f"{key}_first", disabled=page <= 1, on_click=_go_to_page, args=(key, 1))
    prev.button("‹ 이전", key=f"{key}_prev", disabled=page <= 1, on_click=_go_to_page, args=(key, page - 1))
    middle.markdown(f"**{page} / {total_pages}** 페이지")
    nxt.button("다음 ›", key=f"{key}_next", disabled=page >= total_pages, on_click=_go_to_page, args=(key, page + 1))
    last.button("마지막 »", key=f"{key}_last", disabled=page >= total_pages, on_click=_go_to_page, args=(key, total_pages))


def show_paged_table(rows: list[dict[str, str]], columns: list[str], key: str, signature: object) -> None:
    page, total_pages = pager_state(key, signature, len(rows))
    start = (page - 1) * PAGE_SIZE
    show_table(rows[start : start + PAGE_SIZE], columns)
    if total_pages > 1:
        st.caption(f"{start + 1:,}-{min(start + PAGE_SIZE, len(rows)):,}번째 / 전체 {len(rows):,}개")
    show_pager(key, page, total_pages)


def status_badge(status: str) -> str:
    return f":{STATUS_COLORS.get(status, 'gray')}-badge[{status}]"


def show_club_card(row: dict[str, str]) -> None:
    with st.container(border=True):
        category = md_escape(row["구분"] or "분야 미기재")
        st.markdown(f"**{md_escape(row['동아리명'])}** &nbsp; :blue-badge[{category}] {status_badge(row['일정 상태'])}")

        schedule_parts = []
        frequency = " ".join(part for part in (row["정규화 주기"], row["주차"]) if part)
        if frequency:
            schedule_parts.append(f":material/repeat: {md_escape(frequency)}")
        if row["요일"]:
            schedule_parts.append(f":material/calendar_today: {md_escape(row['요일'])}")
        if row["시작 시각"]:
            schedule_parts.append(f":material/schedule: {row['시작 시각']}")
        if schedule_parts:
            st.markdown(" &nbsp;·&nbsp; ".join(schedule_parts))
        else:
            st.caption("확인 가능한 일정 정보 없음")

        st.caption(
            f"원문 — 주기: {md_escape(row['모임주기'] or '정보 없음')} · "
            f"시간: {md_escape(row['모임시간'] or '정보 없음')} · "
            f"결성일: {md_escape(row['결성일자'] or '미기재')}"
        )
        if row["동아리설명"]:
            st.markdown(md_escape(row["동아리설명"]))
        else:
            st.caption("설명 없음")
        if row["검토 메모"]:
            st.caption(f":orange[:material/warning: {md_escape(row['검토 메모'])}]")


st.title(":material/event: 모임ON")
st.caption("옥천군 평생학습 동아리의 모임 정보를 원문과 함께 살펴보는 탐색기")

data_slot = st.empty()
with st.sidebar:
    st.markdown("**데이터 파일 바꾸기**")
    uploaded = st.file_uploader("CSV 파일", type=["csv"], help="기본으로 제공된 동아리 목록을 불러옵니다.")
    st.caption("파일을 올리지 않으면 기본으로 제공된 동아리 목록을 사용합니다. 올린 파일을 지우면 기본 목록으로 돌아갑니다.")

try:
    if uploaded is not None:
        raw_rows = read_csv_bytes(uploaded.getvalue())
        source_label = uploaded.name
    else:
        if not DEFAULT_CSV.exists():
            st.error("기본 CSV 파일을 찾지 못했습니다. 프로젝트 폴더의 data 폴더를 확인해 주세요.")
            st.stop()
        raw_rows = read_csv_bytes(DEFAULT_CSV.read_bytes())
        source_label = DEFAULT_CSV.name
except Exception as exc:
    st.error(f"CSV를 읽지 못했습니다: {exc}")
    st.stop()

if not raw_rows:
    st.warning("CSV에 데이터 행이 없습니다. 다른 파일을 올려 주세요.")
    st.stop()

rows = build_rows(raw_rows)
criteria = sorted({row["데이터기준일자"] for row in rows} - {""})

data_slot.caption(
    f":material/description: {md_escape(source_label)} · 기준일 {', '.join(criteria) if criteria else '미기재'} · {len(rows):,}개 행"
    " — 제공된 목록 기준이며 실제 활동 여부나 일정 유효성은 확인하지 않습니다."
)

tab_find, tab_schedule, tab_quality = st.tabs(
    [":material/search: 동아리 찾기", ":material/calendar_month: 일정 탐색", ":material/fact_check: 데이터 점검"]
)

with tab_find:
    category_counts_all = Counter(row["구분"] for row in rows if row["구분"])
    categories = ["전체"] + sorted(category_counts_all)
    with st.container(border=True):
        selected_category = (
            st.pills(
                "분야",
                categories,
                default="전체",
                format_func=lambda item: f"{item} ({len(rows) if item == '전체' else category_counts_all[item]})",
            )
            or "전체"
        )
        query = st.text_input("이름 또는 설명 검색", placeholder="예: 기타, 춤, 글씨", icon=":material/search:")

    results = rows
    if selected_category != "전체":
        results = [row for row in results if row["구분"] == selected_category]
    if query.strip():
        needle = query.strip().casefold()
        results = [row for row in results if needle in (row["동아리명"] + " " + row["동아리설명"]).casefold()]

    if not results:
        st.info("조건에 맞는 동아리가 없습니다.")
    else:
        page, total_pages = pager_state("page_find", (source_label, selected_category, query.strip()), len(results))
        start = (page - 1) * PAGE_SIZE
        suffix = (
            f" · {start + 1:,}-{min(start + PAGE_SIZE, len(results)):,}번째 · {page}/{total_pages}페이지"
            if total_pages > 1
            else ""
        )
        st.markdown(f"검색 결과 **{len(results):,}개**{suffix}")
        for row in results[start : start + PAGE_SIZE]:
            show_club_card(row)
        show_pager("page_find", page, total_pages)

with tab_schedule:
    st.caption(
        ":material/info: 요일·시각은 모임주기와 모임시간의 원문에서 명시적으로 읽히는 값만 표시합니다. "
        "표시된 일정의 현재 유효성은 확인되지 않았습니다."
    )
    with st.container(border=True):
        chosen_days = st.pills("요일", WEEKDAYS, selection_mode="multi")
        chosen_band = st.segmented_control("시간대", ["전체", "오전", "오후", "저녁"], default="전체") or "전체"

    sched = [row for row in rows if row["일정 상태"] in ("일정 정보 구조화", "일부 정보 추출")]
    if chosen_days:
        sched = [row for row in sched if any(day in row["요일"].split(", ") for day in chosen_days)]
    if chosen_band != "전체":
        sched = [row for row in sched if row["시간대"] == chosen_band]

    st.markdown(f"조건에 맞는 항목 **{len(sched):,}개**")
    if not sched:
        st.info("조건에 맞는 구조화된 일정 정보가 없습니다. 일정 정보가 불완전한 동아리는 아래 검토 목록에서 확인할 수 있습니다.")
    else:
        show_paged_table(
            sched,
            ["동아리명", "구분", "정규화 주기", "주차", "요일", "시작 시각", "일정 상태", "모임주기", "모임시간"],
            "page_schedule",
            (source_label, tuple(chosen_days or []), chosen_band),
        )

    needs_review = [row for row in rows if row["일정 상태"] in ("검토 필요", "정보 없음")]
    with st.expander(f"검토 또는 정보 보완이 필요한 항목 {len(needs_review):,}개", icon=":material/rule:"):
        show_paged_table(needs_review, ["동아리명", "모임주기", "모임시간", "일정 상태", "검토 메모"], "page_review", source_label)

with tab_quality:
    full_row_counts = Counter(tuple(row[column] for column in EXPECTED_COLUMNS) for row in rows)
    name_counts = Counter(row["동아리명"] for row in rows)
    exact_duplicate_count = sum(count - 1 for count in full_row_counts.values())
    status_counts = Counter(row["일정 상태"] for row in rows)

    metric_cols = st.columns(4)
    metric_cols[0].metric("목록 행 수", f"{len(rows):,}", border=True)
    metric_cols[1].metric("완전 중복 추가 행", f"{exact_duplicate_count:,}", border=True)
    metric_cols[2].metric("일정 정보 구조화", f"{status_counts['일정 정보 구조화']:,}", border=True)
    metric_cols[3].metric("검토 필요", f"{status_counts['검토 필요']:,}", border=True)

    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.markdown("**분야별 등록 행 수**")
            category_counts = Counter(row["구분"] or "미분류" for row in rows)
            show_bars(sorted(category_counts.items(), key=lambda item: (-item[1], item[0])), "분야")
    with right:
        with st.container(border=True):
            st.markdown("**주요 필드 결측**")
            show_table(
                [{"항목": column, "결측 건수": str(sum(not row[column] for row in rows))} for column in REVIEW_FIELDS],
                ["항목", "결측 건수"],
            )
        with st.container(border=True):
            st.markdown("**일정 해석 결과 분포**")
            show_table(
                [
                    {"일정 상태": status, "행 수": str(count), "비율": f"{count / len(rows):.1%}"}
                    for status, count in sorted(status_counts.items(), key=lambda item: -item[1])
                ],
                ["일정 상태", "행 수", "비율"],
            )
            st.caption("규칙 기반 해석 결과의 분포이며, 해석된 일정이 맞는지는 원문 확인이 필요합니다.")

    with st.container(border=True):
        st.markdown("**결성 연도별 등록 행 수**")
        year_counts: Counter[str] = Counter()
        for row in rows:
            match = re.fullmatch(r"(\d{4})-\d{2}-\d{2}", row["결성일자"])
            if match:
                year_counts[match.group(1)] += 1
        if not year_counts:
            st.info("유효한 결성 연도 정보가 없습니다.")
        else:
            show_bars(sorted(year_counts.items()), "결성 연도")

    duplicate_names = sorted((row for row in rows if name_counts[row["동아리명"]] > 1), key=lambda row: row["동아리명"])
    if duplicate_names:
        with st.container(border=True):
            st.markdown(f"**동아리명 중복 후보 ({len(duplicate_names):,}개 행)**")
            show_table(duplicate_names, ["동아리명", "구분", "결성일자", "모임주기", "모임시간"])

    review_rows = [
        row
        for row in rows
        if row["일정 상태"] == "검토 필요"
        or any(not row[field] for field in REVIEW_FIELDS)
        or full_row_counts[tuple(row[column] for column in EXPECTED_COLUMNS)] > 1
        or name_counts[row["동아리명"]] > 1
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPECTED_COLUMNS + PARSED_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(review_rows)
    st.download_button(
        f"검토 대상 CSV 다운로드 ({len(review_rows):,}행)",
        data=buffer.getvalue().encode("utf-8-sig"),
        file_name="moimon_review.csv",
        mime="text/csv",
        icon=":material/download:",
    )

    st.warning(
        "**해석 주의**\n\n"
        "이 화면은 제공된 목록의 결측과 표기 상태를 요약합니다. 분야별 등록 수는 주민 수요나 참여도, "
        "결성 연도별 행 수는 현재 활동량을 뜻하지 않습니다."
    )


@st.cache_data(ttl=10, show_spinner=False)
def installed_models() -> list[str] | None:
    return list_models()


# 챗봇은 화면 우측 하단에 떠 있는 아이콘 버튼으로 엽니다. 위치와 세로형 크기만 CSS로 지정합니다.
# (.st-key-chat_fab = key가 chat_fab인 버튼, stPopoverBody = 그 버튼이 여는 창)
st.html(
    """<style>
.st-key-chat_fab { position: fixed; right: 1.5rem; bottom: 1.5rem; z-index: 1000; width: auto; }
.st-key-chat_fab button { border-radius: 999px; padding: 0.75rem 1.1rem; box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35); }
[data-testid="stPopoverBody"] { width: min(360px, 92vw); }
</style>"""
)

with st.popover("챗봇", icon=":material/chat:", type="primary", key="chat_fab", width="content"):
    st.caption(":material/info: 동아리 목록에 있는 내용만 답합니다. 작은 모델이라 틀릴 수 있으니 일정은 원문을 확인해 주세요.")
    history = st.session_state.setdefault("chat", [])

    available = installed_models()
    if available is None:
        st.warning("Ollama 서버에 연결할 수 없습니다. Ollama를 실행한 뒤 다시 시도해 주세요.")
    elif DEFAULT_MODEL not in available:
        st.warning(f"모델 '{DEFAULT_MODEL}'이(가) 설치되어 있지 않습니다. 터미널에서 `ollama pull {DEFAULT_MODEL}` 을 실행해 주세요.")

    messages_box = st.container(height=300, border=False)  # 세로로 긴 대화 영역 (넘치면 스크롤)
    with messages_box:
        if not history:
            st.caption("예) 화요일 저녁에 하는 동아리 알려줘\n\n예) 기타 배울 수 있는 곳 있어?\n\n예) 어학 관련 동아리는?")
        for message in history:
            with st.chat_message(message["role"]):
                st.markdown(md_escape(message["content"]) if message["role"] == "user" else message["content"])
                if message.get("sources"):
                    st.caption(":material/search: 참고한 동아리: " + ", ".join(md_escape(name) for name in message["sources"]))

    if question := st.chat_input("동아리에 대해 물어보세요"):
        canned = canned_reply(question, rows) or direct_reply(question, rows, md_escape)  # 모델을 거치지 않는 답
        messages, hits = build_messages(history, question, rows)
        with messages_box:
            with st.chat_message("user"):
                st.markdown(md_escape(question))
            with st.chat_message("assistant"):
                answer, sources = "", [] if canned else [row["동아리명"] for row in hits]
                slot = st.empty()  # st.write_stream은 pandas를 불러오므로 직접 이어 붙입니다.
                try:
                    if canned:
                        answer = canned
                    else:
                        for piece in stream_chat(messages, DEFAULT_MODEL):
                            answer += piece
                            trimmed = trim_repetition(answer)
                            if trimmed is not None:  # 같은 말을 되풀이하기 시작하면 거기서 끊습니다.
                                answer = trimmed
                                break
                            slot.markdown(answer + " ▌")
                    slot.markdown(answer)
                except ChatError as exc:
                    slot.empty()
                    st.error(str(exc))
                if answer and sources:
                    st.caption(":material/search: 참고한 동아리: " + ", ".join(md_escape(name) for name in sources))
        if answer:
            history += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer, "sources": sources},
            ]

    if history and st.button("대화 지우기", icon=":material/delete:", type="tertiary"):
        history.clear()
        st.rerun()

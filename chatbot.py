"""Ollama 기반 동아리 안내 챗봇 보조 모듈 (표준 라이브러리만 사용).

작은 모델(exaone3.5:2.4b)은 긴 목록을 통째로 읽기 어려우므로, 질문과 관련된 동아리 몇 개만
골라 프롬프트에 넣습니다. 모델에는 제공된 자료에 있는 내용만 말하도록 지시합니다.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, timedelta
from collections.abc import Iterable, Iterator

from schedule_parser import WEEKDAYS

DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "exaone3.5:2.4b")
_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_HOST = _host if _host.startswith("http") else f"http://{_host}"

TOP_K = 5
SCHEDULE_K = 8  # 요일·시간대로 찾을 때는 한 줄을 짧게 보여 주므로 조금 더 많이 넘깁니다.
HISTORY_MESSAGES = 2  # 작은 모델은 이전 대화에 쉽게 끌려가므로 직전 한 턴만 넘깁니다.
DESCRIPTION_LIMIT = 160
_CSV_COLUMNS = ("구분", "동아리명", "결성일자", "모임주기", "모임시간", "동아리설명", "데이터기준일자")

SYSTEM_PROMPT = """\
당신은 옥천군 평생학습 동아리 안내 도우미입니다. 한국어 높임말로, 짧고 읽기 쉽게 답하세요.

가장 중요한 규칙:
1. [동아리 정보]에 적힌 내용만 사용하세요. 없는 내용은 지어내지 마세요.
2. 이모지, 굵은 글씨(**), 표, 글머리표는 쓰지 마세요.

동아리를 안내할 때:
- 질문과 맞는 동아리가 있으면 거절하지 말고 아래 형식으로 쓰세요. 동아리 사이에는 빈 줄을 넣으세요.
  동아리명 (분야)
  일정: 모임주기, 모임시간
  소개: 한 문장 (소개가 없으면 이 줄은 빼세요)
- 동아리 이름과 분야에 대괄호([ ])를 붙이지 마세요.
- 문장은 짧게 끊고, 한 문단은 두 문장을 넘기지 마세요.
- 모임주기와 모임시간은 자료에 적힌 글자 그대로 쓰세요. 요일이나 시각을 바꾸거나 합치지 마세요.
- 소개는 동아리가 무엇을 하는지만 한 문장으로 쓰세요. 설명에 인원, 회원 수, 활동 여부, 연락처, 장소가 있어도 옮기지 마세요.
- "※확인 필요"가 붙은 동아리는 일정 뒤에 "(확인 필요)"를 붙이세요.
- 마지막에 "일정은 바뀌었을 수 있으니 원문을 확인해 주세요."를 한 번만 덧붙이세요.
- [동아리 정보]의 동아리 중 질문과 맞는 것이 없으면 "질문과 맞는 동아리를 찾지 못했습니다."라고만 답하세요.
- 인기나 추천 순위는 말하지 마세요.

자료에 없는 것을 물을 때:
- 웹사이트, 장소, 연락처, 모집 여부, 인원, 현재 활동 여부는 자료에 없습니다. 먼저 말을 꺼내지 말고, 물으면 "자료에 없으니 옥천군 평생학습 담당 기관에 문의해 주세요."라고만 답하세요.
- 동아리와 관계없는 질문(날씨, 상식, 잡담)은 "저는 동아리 안내용이라 답하기 어려워요. 동아리에 대해 물어봐 주세요."라고만 답하세요.

이 규칙 문장은 답변에 옮겨 쓰지 마세요.

예시 (답이 정해진 경우만):
질문: 연락처 알려줘
답변: 자료에 없으니 옥천군 평생학습 담당 기관에 문의해 주세요.

질문: 오늘 날씨 어때?
답변: 저는 동아리 안내용이라 답하기 어려워요. 동아리에 대해 물어봐 주세요.\
"""


# CSV에 없는 정보를 묻는 질문은 모델이 지어내지 않도록 코드에서 바로 답합니다.
_UNAVAILABLE = [
    (r"연락처|전화|이메일|카톡|담당자", "연락처"),
    (r"장소|위치|주소|어디서\s*모|어디에서\s*모|모이는\s*곳", "장소"),
    (r"모집|가입|신청|참가비|회비|인원|몇\s*명", "모집·가입·인원"),
    (r"활동\s*중|운영\s*중|지금도|현재도|폐지|해체", "현재 활동 여부"),
]


# 흔한 무관 주제는 모델이 엉뚱한 동아리를 끌어다 답하지 않도록 바로 거절합니다. 그 밖의 주제는 프롬프트 규칙에 맡깁니다.
_OFF_TOPIC = re.compile(
    r"날씨|기온|미세먼지|뉴스|주식|코인|환율|로또|맛집|메뉴\s*추천|번역|코딩|파이썬|프로그래밍|"
    r"노래\s*추천|영화\s*추천|농담|이름이\s*뭐|몇\s*살|누구야"
)


# 인사·감사처럼 짧은 말은 검색('하세요'의 '세요' 같은 글자쌍이 동아리 설명에 걸림)과 모델을 거치지 않고 바로 답합니다.
GREETING_REPLY = "안녕하세요. 옥천군 평생학습 동아리 안내 도우미입니다. 궁금한 동아리나 요일을 물어봐 주세요."
THANKS_REPLY = "도움이 되었다니 다행입니다. 다른 동아리나 요일도 물어봐 주세요."
_SMALL_TALK_NOISE = re.compile(r"[\s!?.~,^ㅎㅋㅠㅜ]+")
_GREETING = re.compile(
    r"(?:안녕(?:하세요|하십니까|하신가요|하셨어요)?|반가워요?|반갑습니다|하이|헬로|hi|hello|ㅎㅇ|처음뵙겠습니다|"
    r"저기요|여보세요|도우미야?|챗봇아?)+"
)
_THANKS = re.compile(r"(?:고마워요?|고맙습니다|감사합니다|감사해요|감사|땡큐|thanks|thankyou|ㄱㅅ|잘알겠습니다|알겠습니다|알겠어요?|네|넵|예|오케이|ok)+")


def small_talk_reply(question: str) -> str | None:
    """인사나 감사 표현'만' 있으면 고정 문구를, 질문이 섞여 있으면 None을 돌려줍니다."""
    text = _SMALL_TALK_NOISE.sub("", question.casefold())
    if not text:
        return None
    if _GREETING.fullmatch(text):
        return GREETING_REPLY
    if _THANKS.fullmatch(text):
        return THANKS_REPLY
    return None


def _mentioned_in_data(term: str, rows: list[dict[str, str]]) -> bool:
    needle = _compact(term)
    return any(needle in _compact(f"{row['동아리명']} {row['구분']} {row['동아리설명']}") for row in rows)


def canned_reply(question: str, rows: list[dict[str, str]] | None = None) -> str | None:
    small_talk = small_talk_reply(question)
    if small_talk:
        return small_talk
    off_topic = [match.group(0) for match in _OFF_TOPIC.finditer(question)]
    # '코딩', '번역'처럼 무관 주제 단어라도 목록의 동아리 이름·분야·설명에 있으면 동아리 질문일 수 있으니 거절하지 않습니다.
    if off_topic and not (rows and any(_mentioned_in_data(term, rows) for term in off_topic)):
        return "저는 옥천군 평생학습 동아리 안내용이라 그 질문에는 답해 드릴 수 없어요. 동아리에 대해 물어봐 주세요."
    labels = [label for pattern, label in _UNAVAILABLE if re.search(pattern, question)]
    if not labels:
        # 막연한 추천 요청·검색 결과 없음은 모델을 부르지 않습니다(연락처 같은 질문은 위에서 먼저 답합니다).
        return fallback_reply(question, rows) if rows else None
    return (
        f"{', '.join(labels)} 정보는 제공된 자료에 없습니다. 옥천군 평생학습 담당 기관에 문의해 주세요. "
        "동아리 이름, 분야, 모임 일정은 물어봐 주세요."
    )


_SENTENCE = re.compile(r"[^.!?\n]+[.!?\n]+")


def trim_repetition(text: str) -> str | None:
    """같은 문장이 3번 이상 나오거나 끝부분이 같은 구절의 반복이면 첫 번째까지만 남긴 글을, 아니면 None을 돌려줍니다."""
    # 1) 완성된 문장 단위: 같은 문장이 3번째 나오면 두 번째 등장 앞에서 자릅니다.
    seen: dict[str, list[int]] = {}
    for match in _SENTENCE.finditer(text):
        sentence = match.group(0).strip()
        if len(sentence) >= 8:
            seen.setdefault(sentence, []).append(match.start())
    cuts = [starts[1] for starts in seen.values() if len(starts) >= 3]
    if cuts:
        return text[: min(cuts)].rstrip()

    # 2) 문장부호 없이 같은 구절이 이어지는 경우: 끝의 구절이 3번 반복되면 한 번만 남기고, 가까운 문장 끝에서 자릅니다.
    for size in range(8, min(200, len(text) // 3) + 1):
        if text.endswith(text[-size:] * 3):
            kept = text[: len(text) - 2 * size].rstrip()
            boundary = max(kept.rfind(mark) for mark in ".!?\n")
            return kept[: boundary + 1] if boundary >= len(kept) // 2 else kept
    return None


class ChatError(RuntimeError):
    """사용자에게 그대로 보여 줄 수 있는 챗봇 오류."""


# --- 검색 -------------------------------------------------------------------


_QUESTION_WORDS = re.compile(
    r"알려\s*주세요|알려\s*줘|알려\s*줄래|있나요|있을까요?|있어요?|있는|하는|해\s*줘|추천|궁금|관련|어디|무엇|뭐|"
    r"배울\s*수|할\s*수|동아리|모임|곳|좀|모이는|모여요|모여|어떤|무슨|해요|하나요|뭐가|있지|있나|있니|있죠|있을까"
)


_FOLLOW_UP = re.compile(r"거기|그곳|그\s*동아리|이\s*동아리|그럼|그중|저기|그거")


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.casefold())


def _bigrams(text: str) -> set[str]:
    compact = _compact(_QUESTION_WORDS.sub(" ", text.casefold()))
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def _document(row: dict[str, str]) -> tuple[str, str]:
    """(이름·분야 텍스트, 나머지 텍스트). 요일은 '화요일'처럼 풀어서 검색에 걸리게 합니다."""
    days = " ".join(f"{day}요일" for day in row["요일"].split(", ") if day)
    head = f"{row['동아리명']} {row['구분']}"
    body = " ".join(
        [row["동아리설명"], row["모임주기"], row["모임시간"], row["정규화 주기"], row["주차"], days, row["시간대"]]
    )
    return head, body


def rank(question: str, rows: list[dict[str, str]]) -> list[tuple[float, dict[str, str]]]:
    """(점수, 동아리)를 점수 높은 순으로. 겹침이 전혀 없으면 빈 목록입니다."""
    # 질문에 동아리 이름이 그대로 들어 있으면 그 동아리만 고릅니다(이름이 비슷한 다른 동아리와 헷갈리지 않게).
    asked = _compact(question)
    exact = [row for row in rows if len(_compact(row["동아리명"])) >= 3 and _compact(row["동아리명"]) in asked]
    if exact:
        return [(float("inf"), row) for row in exact]

    query = _bigrams(question)
    if not query or not rows:
        return []
    docs = [_document(row) for row in rows]
    doc_sets = [(_bigrams(head), _bigrams(body)) for head, body in docs]
    frequency = Counter(gram for head, body in doc_sets for gram in head | body)
    total = len(rows)

    def weight(gram: str) -> float:  # 어느 문서에나 있는 글자쌍(예: '입니')은 가중치를 낮춥니다.
        return math.log((total + 1) / (frequency[gram] + 1)) + 0.1

    scored = []
    for row, (head, body) in zip(rows, doc_sets):
        score = sum(weight(g) * (3 if g in head else 1) for g in query if g in head or g in body)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: -item[0])
    return scored


def _unique(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """CSV의 모든 열이 같은 행(완전 중복)은 한 번만 남깁니다."""
    seen: set[tuple[str, ...]] = set()
    result = []
    for row in rows:
        key = tuple(row.get(column, "") for column in _CSV_COLUMNS)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def retrieve(question: str, rows: list[dict[str, str]], k: int = TOP_K) -> list[dict[str, str]]:
    scored = rank(question, rows)
    if not scored:
        return []
    cutoff = scored[0][0] * 0.4  # 1등과 점수 차이가 큰 우연한 일치는 버립니다.
    return _unique(row for score, row in scored if score >= cutoff)[:k]


# '화요일 저녁'처럼 요일·시간대를 묻는 질문은 글자쌍 검색('요일'이 모든 행에 겹침)이 아니라 구조화된 값으로 거릅니다.
_DAY_TOKEN = re.compile(r"([월화수목금토일])\s*요일")
_BAND_WORDS = {"오전": "오전", "아침": "오전", "오후": "오후", "저녁": "저녁"}
_FILLER_ONLY = re.compile(
    r"(?:인가요|인가|인지|입니까|이에요|예요|무엇|있는지|있을까요|있나요|있어요|있지|있나|있니|있죠|뭐해|뭐|뭘|해요|해|날|엔|에는|에서|에|은|는|이|가|을|를|도|만|들|좀|요|죠|지|야|나|까)+"
)
_SCHEDULE_NOISE = re.compile(r"[월화수목금토일]\s*요일|오늘|내일|모레|주말|오전|아침|오후|저녁|요일|이나|또는|그리고|랑|하고")


_RELATIVE_DAYS = {"오늘": 0, "내일": 1, "모레": 2}


def _relative_days(question: str, today: date | None = None) -> set[str]:
    """'오늘', '내일', '모레'를 실제 요일로 바꿉니다(서버 시계 기준)."""
    today = today or date.today()
    return {"월화수목금토일"[(today + timedelta(days=offset)).weekday()]
            for word, offset in _RELATIVE_DAYS.items() if word in question}


def schedule_filter(question: str) -> tuple[set[str], set[str]]:
    """질문에서 (요일 집합, 시간대 집합)을 읽습니다. 없으면 빈 집합입니다."""
    days = {day for day in _DAY_TOKEN.findall(question) if day in WEEKDAYS}
    days |= {day for day in _relative_days(question) if day in WEEKDAYS}
    if "주말" in question:
        days |= {"토", "일"}
    bands = {band for word, band in _BAND_WORDS.items() if word in question}
    return days, bands


def _schedule_candidates(rows: list[dict[str, str]], days: set[str], bands: set[str]) -> list[dict[str, str]]:
    return [
        row
        for row in rows
        if (not days or days & set(row["요일"].split(", "))) and (not bands or row["시간대"] in bands)
    ]


def _schedule_only(stripped_question: str) -> bool:
    """요일·시간대를 떼어 낸 뒤 '날', '에', '인지' 같은 조사·어미만 남으면 요일·시간대만 물은 질문입니다."""
    leftover = [_compact(token) for token in _QUESTION_WORDS.sub(" ", stripped_question.casefold()).split()]
    return all(_FILLER_ONLY.fullmatch(token) for token in leftover if token)  # '도예'처럼 주제어가 있으면 아닙니다


def search(question: str, rows: list[dict[str, str]], k: int = TOP_K) -> tuple[list[dict[str, str]], int, bool]:
    """(보여 줄 동아리, 조건에 맞는 전체 개수, 한 줄을 짧게 보일지)를 돌려줍니다."""
    days, bands = schedule_filter(question)
    if not (days or bands):
        hits = retrieve(question, rows, k)
        return hits, len(hits), False

    candidates = _schedule_candidates(rows, days, bands)
    stripped = _SCHEDULE_NOISE.sub(" ", question)
    if _schedule_only(stripped):
        unique = _unique(candidates)
        return unique[:SCHEDULE_K], len(unique), True
    hits = retrieve(stripped, candidates, k)  # '화요일 저녁 기타'처럼 주제도 있으면 조건에 맞는 동아리 안에서만 찾습니다.
    return hits, len(hits), False


DIRECT_LIMIT = 30


def direct_reply(question: str, rows: list[dict[str, str]], escape=lambda text: text) -> str | None:
    """'화요일 저녁에 하는 동아리'처럼 요일·시간대만 묻는 질문은 모델 없이 목록에서 바로 답합니다.

    작은 모델은 이런 목록 질문에도 "자료에 없다"고 답하거나 개수를 지어내므로, 코드가 거른 결과를 그대로 보여 줍니다.
    그 밖의 질문이면 None을 돌려줍니다.
    """
    days, bands = schedule_filter(question)
    if not (days or bands) or not _schedule_only(_SCHEDULE_NOISE.sub(" ", question)):
        return None

    parts = [word for word in _RELATIVE_DAYS if word in question]
    names = [day for day in WEEKDAYS if day in days]
    if "주말" in question and set(names) == {"토", "일"}:
        parts.append("주말")
    elif names:
        parts.append(("(" if parts else "") + "·".join(names) + "요일" + (")" if parts else ""))
    if bands:
        parts.append("·".join(band for band in ("오전", "오후", "저녁") if band in bands))
    label = " ".join(parts).replace(" (", "(")

    seen: set[tuple[str, str, str]] = set()
    matches = []
    for row in _unique(_schedule_candidates(rows, days, bands)):  # 같은 동아리가 같은 일정으로 두 번 올라온 행은 한 번만 보입니다.
        key = (row["동아리명"], row["모임주기"], row["모임시간"])
        if key not in seen:
            seen.add(key)
            matches.append(row)
    if not matches:
        return (
            f"{label}에 모이는 것으로 확인되는 동아리를 목록에서 찾지 못했습니다. "
            "모임 요일이나 시간이 적혀 있지 않은 동아리는 포함되지 않습니다."
        )
    lines = [f"{label}에 모이는 동아리는 모두 {len(matches)}개입니다.", ""]
    for row in matches[:DIRECT_LIMIT]:
        schedule = ", ".join(dict.fromkeys(part for part in (row["모임주기"], row["모임시간"]) if part))
        caution = " (일정 확인 필요)" if row["일정 상태"] in ("검토 필요", "정보 없음") else ""
        lines.append(f"- {escape(row['동아리명'])} ({escape(row['구분'] or '분야 미기재')}): {escape(schedule)}{caution}")
    if len(matches) > DIRECT_LIMIT:
        lines.append(f"- 그 밖에 {len(matches) - DIRECT_LIMIT}개가 더 있습니다. 분야나 키워드를 함께 물어보시면 좁혀 드릴게요.")
    lines += ["", "일정은 목록의 원문 기준이며, 지금도 유효한지는 확인되지 않았습니다."]
    return "\n".join(lines)


# '이번 주 동아리 추천해 줘', '아무거나'처럼 주제·요일 없이 막연하게 묻는 말입니다.
_GENERIC_WORDS = re.compile(
    r"이번\s*주|이번\s*달|요즘|최근|아무\s*거나|아무\s*거|아무|전부|전체|모든|모두|목록|리스트|종류|"
    r"좋은|괜찮은|재밌는|재미있는|재미난|인기|추천|소개|알려|해\s*주세요|해\s*줘|해\s*줄래|해\s*달라|달라고|달라|"
    r"주실래요|주시겠어요|줄래요|줄래|주세요|줘|부탁|보여|"
    r"참여|가입|들어갈|갈\s*만한|만한|할\s*만한|가능한|어떤\s*거|있는\s*거|거|것"
)


def _is_generic(question: str) -> bool:
    # '알려 줘'가 '알려'+'줘'로 쪼개지지 않도록 질문 표현을 먼저 지웁니다.
    text = _QUESTION_WORDS.sub(" ", question.casefold())
    leftover = [_compact(token) for token in _GENERIC_WORDS.sub(" ", text).split()]
    return all(_FILLER_ONLY.fullmatch(token) for token in leftover if token)


def _guide_reply(rows: list[dict[str, str]], lead: str) -> str:
    categories = Counter(row["구분"] or "분야 미기재" for row in _unique(rows))
    lines = [lead, "", "분야별 동아리 수"]
    lines += [f"- {name} {count}개" for name, count in categories.most_common()]
    lines += ["", "이렇게 물어봐 주세요", "- 금요일 동아리", "- 오늘 저녁 동아리", f"- {categories.most_common(1)[0][0]} 동아리"]
    return "\n".join(lines)


def fallback_reply(question: str, rows: list[dict[str, str]]) -> str | None:
    """주제·요일이 없는 막연한 질문이나, 검색 결과가 하나도 없는 질문은 모델 없이 안내 문구로 답합니다.

    작은 모델은 자료가 비어 있으면 '[...]' 같은 틀을 지어내므로 이 경우 모델을 부르지 않습니다.
    그 밖의 질문이면 None을 돌려줍니다.
    """
    days, bands = schedule_filter(question)
    if days or bands:
        return None  # 요일·시간대 질문은 direct_reply와 검색이 맡습니다.
    if _is_generic(question):
        return _guide_reply(rows, "분야나 요일을 알려 주시면 맞는 동아리를 찾아 드릴게요.")
    if _FOLLOW_UP.search(question):
        return None  # '거기 시간은?' 같은 후속 질문은 직전 질문과 함께 검색하므로 여기서 판단하지 않습니다.
    if not search(question, rows)[0]:
        return _guide_reply(rows, "질문과 맞는 동아리를 찾지 못했습니다.")
    return None


# --- 프롬프트 -----------------------------------------------------------------


def _club_line(row: dict[str, str], compact: bool = False) -> str:
    # 작은 모델은 구조화 값까지 섞으면 요일·시각을 뒤섞으므로, 원문 중심으로 짧게 보여 줍니다.
    description = row["동아리설명"]
    if len(description) > DESCRIPTION_LIMIT:
        description = description[:DESCRIPTION_LIMIT] + "…"
    caution = " ※확인 필요" if row["일정 상태"] in ("검토 필요", "정보 없음") else ""
    line = (
        f"- {row['동아리명']} ({row['구분'] or '분야 미기재'}) / "
        f"모임주기: {row['모임주기'] or '없음'} / 모임시간: {row['모임시간'] or '없음'}{caution}"
    )
    return line if compact else f"{line} / 소개: {description or '없음'}"


def build_context(
    rows: list[dict[str, str]], hits: list[dict[str, str]], total: int | None = None, compact: bool = False
) -> str:
    reference = reference_date(rows)
    lines = [f"[동아리 정보] 데이터 기준일 {reference}."]
    if hits:
        if total is not None and total > len(hits):
            lines.append(f"질문 조건에 맞는 동아리는 모두 {total}개이며, 그중 {len(hits)}개만 아래에 적었습니다:")
        else:
            lines.append("질문과 관련된 동아리:")
        lines.extend(_club_line(row, compact) for row in hits)
    else:
        lines.append("질문과 관련된 동아리를 찾지 못했습니다.")
    return "\n".join(lines)


def reference_date(rows: list[dict[str, str]]) -> str:
    dates = sorted({row["데이터기준일자"] for row in rows} - {""})
    return ", ".join(dates) if dates else "미기재"


def build_messages(
    history: list[dict[str, str]], question: str, rows: list[dict[str, str]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(Ollama에 보낼 메시지, 참고한 동아리). 직전 사용자 질문도 검색에 함께 써서 후속 질문을 받아 줍니다."""
    # '거기 연락처는?' 같은 후속 질문일 때만 직전 질문을 검색에 함께 씁니다. 주제가 바뀐 질문은 섞지 않습니다.
    follow_up = bool(_FOLLOW_UP.search(question))
    previous = [m["content"] for m in history if m["role"] == "user"][-1:] if follow_up else []
    hits, total, compact = search(" ".join([*previous, question]), rows)
    # 자료는 시스템 지시가 아니라 질문 바로 앞에 붙여야 작은 모델이 놓치지 않고 읽습니다.
    user = f"{build_context(rows, hits, total, compact)}\n\n질문: {question}"
    # 이전 답변은 후속 질문일 때만 넘깁니다. 주제가 바뀐 질문에 이전 답변의 동아리가 섞이는 것을 막습니다.
    recent = [{"role": m["role"], "content": m["content"]} for m in history[-HISTORY_MESSAGES:]] if follow_up else []
    return [{"role": "system", "content": SYSTEM_PROMPT}, *recent, {"role": "user", "content": user}], hits


# --- Ollama 호출 ----------------------------------------------------------------


def list_models(host: str = OLLAMA_HOST) -> list[str] | None:
    """설치된 모델 이름 목록. 서버에 연결할 수 없으면 None."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=2) as response:
            return [item["name"] for item in json.load(response).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _error_message(exc: urllib.error.HTTPError, model: str) -> tuple[str, str]:
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("error", "")
    except (ValueError, OSError):
        detail = ""
    if exc.code == 404:
        return detail, f"모델 '{model}'을(를) 찾을 수 없습니다. 터미널에서 `ollama pull {model}` 을 실행해 주세요."
    return detail, f"Ollama가 오류를 돌려줬습니다 ({exc.code}): {detail or exc.reason}"


def stream_chat(
    messages: list[dict[str, str]], model: str = DEFAULT_MODEL, host: str = OLLAMA_HOST
) -> Iterator[str]:
    """답변을 조각 단위로 돌려줍니다. 실패하면 ChatError를 냅니다."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,  # qwen3 계열의 생각(<think>) 출력을 끕니다.
        # 온도가 너무 낮으면 작은 모델이 같은 문장을 되풀이하므로 약간 올리고, 반복 억제와 답변 길이 상한을 둡니다.
        "options": {
            "temperature": 0.3,
            "top_p": 0.9,
            "repeat_penalty": 1.2,
            "repeat_last_n": 256,
            "num_predict": 400,
            "num_ctx": 4096,
        },
    }
    for attempt in range(2):
        request = urllib.request.Request(
            f"{host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:  # 첫 호출은 모델 로딩이 오래 걸립니다.
                for raw in response:
                    if not raw.strip():
                        continue
                    chunk = json.loads(raw)
                    if chunk.get("error"):
                        raise ChatError(f"Ollama 오류: {chunk['error']}")
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if chunk.get("done"):
                        return
            return
        except urllib.error.HTTPError as exc:
            detail, message = _error_message(exc, model)
            if attempt == 0 and "think" in detail.lower():
                payload.pop("think")  # 생각 기능을 모르는 모델/버전이면 옵션을 빼고 한 번 더 시도합니다.
                continue
            raise ChatError(message) from exc
        except urllib.error.URLError as exc:
            raise ChatError(
                f"Ollama 서버({host})에 연결할 수 없습니다. Ollama를 실행했는지 확인해 주세요."
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise ChatError("Ollama 응답이 너무 오래 걸립니다. 잠시 후 다시 시도해 주세요.") from exc

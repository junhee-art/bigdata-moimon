"""모임주기·모임시간 원문에서 명시적으로 읽히는 값만 보수적으로 추출하는 규칙 기반 파서.

순수 파이썬 모듈이라 단독으로 테스트할 수 있습니다. 원문에 없는 값은 추측하지 않고,
모호하면 빈 문자열로 남겨 검토 대상으로 표시합니다.
"""
from __future__ import annotations

import re

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

# 이 값을 넘는 횟수는 입력 오류일 가능성이 커서 구조화하지 않고 검토로 보냅니다.
MAX_PER_WEEK, MAX_PER_MONTH, MAX_PER_YEAR = 7, 12, 366

_KOREAN_COUNT = {"한": 1, "두": 2, "세": 3, "네": 4}
_COUNT = r"(\d+|한|두|세|네)"
_TIMES = r"\s*(?:회|번)"

# (정규식, 단위, 상한) — 위에서부터 먼저 맞는 규칙을 씁니다. 그룹이 둘이면 'N~M회' 범위입니다.
_FREQUENCY_RULES = [
    (rf"(?:주|주간)\s*(\d+)\s*[~\-]\s*(\d+)\s*회", "주", MAX_PER_WEEK),
    (rf"(?:일주일|한\s*주)\s*(?:에)?\s*{_COUNT}{_TIMES}", "주", MAX_PER_WEEK),  # 일주일에 한 번
    (rf"한\s*달\s*(?:에)?\s*{_COUNT}{_TIMES}", "월", MAX_PER_MONTH),  # 한달한번, 한 달에 두 번
    (r"연\s*(\d+)\s*회", "연", MAX_PER_YEAR),
    (r"(?:주|주간)\s*(\d+)\s*회", "주", MAX_PER_WEEK),
    (r"(?:월|매월|매달)\s*(\d+)\s*[~\-]\s*(\d+)\s*회", "월", MAX_PER_MONTH),
    (r"(?:월|매월|매달)\s*(\d+)\s*회", "월", MAX_PER_MONTH),
]
_EVERY_N_WEEKS = rf"(\d+)\s*주(?:일)?\s*(?:에)?\s*{_COUNT}{_TIMES}"  # 2주일1회, 2주에 한번


def _to_int(token: str) -> int:
    return _KOREAN_COUNT.get(token) or int(token)


def normalize_frequency(raw: str) -> tuple[str, bool, bool]:
    """(정규화 주기, 이해 여부, 비정상 횟수 여부)를 돌려줍니다."""
    value = re.sub(r"\s+", " ", raw.replace("　", " ").replace("（", "(").replace("）", ")")).strip()
    suffix = " 이상" if re.search(r"회\s*이상", value) else ""
    anomaly = ("", False, True)

    match = re.search(_EVERY_N_WEEKS, value)
    if match:
        weeks, times = int(match.group(1)), _to_int(match.group(2))
        if weeks == 0 or times == 0 or (weeks == 1 and times > MAX_PER_WEEK):
            return anomaly
        return (f"주 {times}회" if weeks == 1 else f"{weeks}주 {times}회") + suffix, True, False

    for pattern, unit, limit in _FREQUENCY_RULES:
        match = re.search(pattern, value)
        if match:
            low, high = _to_int(match.group(1)), _to_int(match.groups()[-1])
            if low == 0 or low > high or high > limit:
                return anomaly
            span = f"{low}" if low == high else f"{low}~{high}"
            return f"{unit} {span}회{suffix}", True, False

    if "격주" in value:
        return "격주 (2주 1회)", True, False
    if re.search(r"매달|매월", value):
        return "매월 (횟수 미기재)", True, False
    if "매주" in value:
        return "매주 (횟수 미기재)", True, False
    return "", False, False


def extract_weekdays(text: str) -> list[str]:
    """명시된 요일 토큰만 추출합니다. 날짜나 문장에서 추론하지 않습니다."""
    # 주기·주차 표현을 먼저 지워 '월 1회', '마지막주토요일'의 글자가 요일로 오인되지 않게 합니다.
    value = re.sub(r"매월|매달|매주|격주|월\s*\d+\s*(?:[~\-]\s*\d+\s*)?회", " ", text)
    value = re.sub(r"\d+\s*주일?|(?:마지막|첫|둘|셋|넷|다섯)\s*째?\s*주", " ", value)
    # '~요일'은 그 자체로 명확하고, 한 글자 요일은 앞뒤 경계가 분명할 때만 인정합니다.
    return [
        day
        for day in WEEKDAYS
        if re.search(rf"{day}요일|{day}욜|(?<![가-힣\d]){day}(?=$|[\s,，·/0-9:;~\-])", value)
    ]


_WEEK_NAMES = {"첫": "첫째", "둘": "둘째", "셋": "셋째", "넷": "넷째", "다섯": "다섯째", "마지막": "마지막"}
_WEEK_NAMES.update({str(n): name for n, name in enumerate(["첫째", "둘째", "셋째", "넷째", "다섯째"], start=1)})
# '첫째주', '둘째, 넷째주', '4째주', '2,4주'. '2주일', '2주에', '2주 1회'(격주 표현)는 주차가 아니므로 제외합니다.
_WEEK_GROUP = re.compile(r"((?:(?:마지막|첫|둘|셋|넷|다섯|[1-5])째?[,.·]?)+)주(?!일(?!요일)|에|간)")
_WEEK_TOKEN = re.compile(r"(마지막|첫|둘|셋|넷|다섯|[1-5])(째?)")


def extract_week_of_month(text: str) -> str:
    """'마지막주', '첫째주', '4째주', '둘째, 넷째주'처럼 명시된 주차만 모두 추출합니다."""
    value = re.sub(r"\s+", "", text)
    names: list[str] = []
    for group in _WEEK_GROUP.finditer(value):
        tokens = _WEEK_TOKEN.findall(group.group(1))
        for token, ordinal_mark in tokens:
            # 숫자만 있는 단일 값('2주')은 주차인지 알 수 없으므로 '째'가 붙었거나 '2,4주'처럼 나열된 경우만 인정합니다.
            if token.isdigit() and not ordinal_mark and len(tokens) < 2:
                continue
            name = _WEEK_NAMES[token]
            if name not in names:
                names.append(name)
    return f"{'·'.join(names)} 주" if names else ""


# 분 표기: '30분', '7시30'(분 생략), '1시 반'. '2시간'처럼 뒤에 '시'가 붙는 숫자는 분으로 보지 않습니다.
_MINUTE = r"(?:\s*(?:(\d{1,2})\s*분|(\d{2})(?![\d시])|(반)))?"


def _minutes(minute: str | None, bare_minute: str | None, half: str | None) -> int:
    return 30 if half else int(minute or bare_minute or 0)


def extract_time(text: str) -> str:
    """시작 시각을 'HH:MM'으로 돌려줍니다. 오전/오후가 없는 12시 이하 시각은 모호하므로 비웁니다."""
    value = re.sub(r"\s+", " ", re.sub(r"(오전|오후|저녁)", r" \1 ", text)).strip()

    # 24시간제 표기 (14:00, 19;00). 범위(13:30~15:30)는 앞의 시작 시각만 사용합니다.
    match = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*[:;]\s*([0-5]\d)(?!\d)", value)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    # 오전/오후/저녁이 붙은 한글 표기 (오후 7시, 저녁 7시 30분, 오후 1시 반, 저녁7~8시30분)
    match = re.search(rf"(오전|오후|저녁)\s*(\d{{1,2}})(\s*[~\-]\s*\d{{1,2}})?\s*시(?!간){_MINUTE}", value)
    if match:
        period, hour = match.group(1), int(match.group(2))
        # '7~8시30분'의 분은 끝 시각의 것이므로, 범위일 때는 시작 시각의 분을 0으로 봅니다.
        minute = 0 if match.group(3) else _minutes(match.group(4), match.group(5), match.group(6))
        if not 1 <= hour <= 12 or minute > 59:
            return ""
        if period != "오전" and hour < 12:
            hour += 12
        elif period == "오전" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"

    # 오전/오후가 없는 한글 표기. 처음 나오는 시각만 보며, 13~23시만 24시간제로 읽습니다.
    # 12시 이하는 오전/오후를 알 수 없어 비우고, 뒤에 나오는 끝 시각(예: '12시-14시'의 14시)으로 대신하지 않습니다.
    match = re.search(rf"(?<!\d)(\d{{1,2}})(\s*[~\-]\s*\d{{1,2}})?\s*시(?!간){_MINUTE}", value)
    if match:
        hour = int(match.group(1))
        minute = 0 if match.group(2) else _minutes(match.group(3), match.group(4), match.group(5))
        if 13 <= hour <= 23 and minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return ""


# '독서모임: ..., 철학강연: ...'처럼 이름표가 붙은 일정이 둘 이상이면 한 동아리의 일정이 아니라 여러 모임이 섞인 것입니다.
_LABELED_SEGMENT = re.compile(r"[가-힣A-Za-z]{2,}\s*:\s*(?!\d)")


def _has_multiple_units(frequency_raw: str) -> bool:
    """한 칸에 월 단위와 주 단위 횟수가 함께 적힌 경우 (예: '정기 매월2회 비정기 모임 주 2회')."""
    monthly = re.search(r"(?:월|매월|매달)\s*\d+\s*회", frequency_raw)
    weekly = re.search(r"(?<![\d일])(?:주|주간)\s*\d+\s*회", frequency_raw)
    return bool(monthly and weekly)


def _frequency_conflict(frequency: str, time_raw: str) -> bool:
    """모임주기 칸의 횟수와 모임시간 칸의 주기 표현이 서로 맞지 않는지 봅니다. ('월 1회'인데 '매주 금요일' 등)"""
    match = re.match(r"(월|연|주) (\d+)(?:~(\d+))?회", frequency)
    if not match:
        return False
    unit, high = match.group(1), int(match.group(3) or match.group(2))
    if re.search(r"매\s*주", time_raw):
        return unit == "연" or (unit == "월" and high < 4)  # 매주면 한 달에 4회 안팎이어야 합니다.
    if re.search(r"매월|매달", time_raw):
        return unit in ("주", "연")
    return False


def parse_schedule_text(frequency_raw: str, time_raw: str) -> dict[str, str]:
    frequency_raw, time_raw = (frequency_raw or "").strip(), (time_raw or "").strip()
    combined = f"{frequency_raw} {time_raw}".strip()

    frequency, frequency_known, anomaly = normalize_frequency(frequency_raw)
    conflict = _has_multiple_units(frequency_raw)
    if conflict:
        frequency, frequency_known = "", False
    elif not frequency_known and not anomaly:
        # 주기 칸이 비었거나 모호하면 시간 칸의 명시적 주기 표현(예: '매주 목요일')을 사용합니다.
        frequency, frequency_known, anomaly = normalize_frequency(time_raw)
    elif frequency_known and _frequency_conflict(frequency, time_raw):
        conflict, frequency, frequency_known = True, "", False

    mixed = len(_LABELED_SEGMENT.findall(time_raw)) >= 2
    if mixed:
        weekdays, week_of_month, start_time = [], "", ""
    else:
        weekdays = extract_weekdays(combined)
        week_of_month = extract_week_of_month(combined)
        start_time = extract_time(combined)

    numeric_only = any(re.fullmatch(r"\d+(?:\.\d+)?", raw) for raw in (frequency_raw, time_raw))
    has_duration = bool(re.search(r"\d+\s*시간", combined))
    extracted_any = frequency_known or bool(weekdays or start_time or week_of_month)

    notes = []
    if anomaly:
        notes.append("모임 횟수가 비정상적으로 보여 구조화하지 않았습니다. 원문을 확인해 주세요.")
    if conflict:
        notes.append("모임주기와 모임시간의 주기 표현이 서로 달라 주기를 구조화하지 않았습니다. 원문을 확인해 주세요.")
    if mixed:
        notes.append("여러 모임의 일정이 한 칸에 섞여 있어 구조화하지 않았습니다. 원문을 확인해 주세요.")
    if numeric_only:
        notes.append("숫자만 있는 값은 의미를 알 수 없어 해석하지 않았습니다.")
    if not (start_time or anomaly or mixed) and re.search(r"(?<!\d)\d{1,2}\s*시(?!간)", combined):
        notes.append("오전/오후가 없는 시각은 모호해 해석하지 않았습니다.")

    if not (frequency_raw or time_raw):
        status = "정보 없음"
    elif anomaly or conflict or mixed or (not extracted_any and (numeric_only or has_duration)):
        status = "검토 필요"
    elif frequency_known and (weekdays or start_time or week_of_month):
        status = "일정 정보 구조화"
    elif extracted_any:
        status = "일부 정보 추출"
    else:
        status = "검토 필요"
    if status == "검토 필요" and not notes:
        notes.append("원문을 확인해 주세요.")

    hour = int(start_time[:2]) if start_time else None
    time_band = "" if hour is None else "오전" if hour < 12 else "오후" if hour < 18 else "저녁"

    return {
        "정규화 주기": frequency,
        "요일": ", ".join(weekdays),
        "주차": week_of_month,
        "시작 시각": start_time,
        "시간대": time_band,
        "일정 상태": status,
        "검토 메모": " ".join(notes),
    }

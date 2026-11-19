"""Traceable facts from one current inquiry body; never infer identity from headers."""

import re
from datetime import date

from .extraction_agent import current_body_text_check

DETAIL_FIELDS = frozenset(
    {
        "customer_company",
        "contact_name",
        "contact_phone",
        "signature_email",
        "cargo_ready_time",
        "business_remarks",
    }
)
AGENT_DETAIL_FIELDS = DETAIL_FIELDS | {"cargo_type"}
_SIGNOFF = re.compile(
    r"(?:thanks(?:\s+and)?[ ,]*best regards|best regards|kind regards|regards|best wishes|sincerely|"
    r"saludos(?: cordiales)?|un saludo|顺祝商祺)[\s,，.!。]*",
    re.I,
)
_EMAIL = r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}"
_SIGN_EMAIL = re.compile(rf"(?:(?:e[ -]?mail|mail|邮箱)\s*[:：]\s*)?(?P<v>{_EMAIL})", re.I)
_SIGN_PHONE = re.compile(
    r"(?:tel(?:ephone)?\.?|phone|mobile|whatsapp|联系电话|电话|手机)[\s:：.]+(?P<v>\+?[0-9 ()\-.]{7,28})",
    re.I,
)
_MOBILE_OFFICE = re.compile(
    r"mobile[\s:：.]+(?P<v>\+?[0-9 ()\-.]{7,28})[ \t]+(?:office|tel(?:ephone)?)\b",
    re.I,
)
_COMPANY = re.compile(
    r"(?:\b(?:co\.?\s*,?\s*ltd\.?|ltd\.?|inc\.?|llc|limited|logistics|gmbh)\b|有限公司|物流公司)",
    re.I,
)
_ROLE_COMPANY = re.compile(
    r"[^/@\d]{1,70}\b(?:department|division)\s*/\s*(?P<v>[^/@:：]{2,100})", re.I
)
_NAME = re.compile(
    r"(?:[A-ZÀ-ÖØ-Þ][\w'’.-]+(?:[ \t]+[A-ZÀ-ÖØ-Þ][\w'’.-]+){1,3}|[\u4e00-\u9fff]{2,4})"
)
_NAME_BLOCKLIST = re.compile(
    r"\b(?:manager|sales|team|service|support|department|office|logistics)\b", re.I
)
_MONTH = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_NAMED_DAY = re.compile(
    rf"(\d{{1,2}})(?:st|nd|rd|th)?[\s/,.-]+({_MONTH})(?:[\s/,.-]+(\d{{4}}))?", re.I
)
_MONTHS = {
    name: i for i, name in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)
}
_READY = re.compile(
    r"(?:\b(?:cargo[ \t]+ready(?:[ \t]+date)?|ready[ \t]+date|readiness[ \t]+date|"
    r"(?:will|would)[ \t]+(?:only[ \t]+)?be[ \t]+ready)(?:[ \t]+(?:on|by))?"
    r"(?:[ \t]*[:：,][ \t]*|[ \t]+)|(?:^ready[ \t]*[:：]|货好(?:时间|日期)[ \t]*[:：])[ \t]*)"
    rf"(?P<value>\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}}|\d{{1,2}}/\d{{1,2}}(?:/\d{{4}})?|{_NAMED_DAY.pattern})"
    r"(?![\d/])[ \t]*[.。]?[ \t]*$",
    re.I,
)
_UNCERTAIN_DATE = re.compile(
    r"\b(?:if|tentative|estimated|not|or|when|maybe|unconfirmed)\b|预计|暂定|待定|尚未|如果|若",
    re.I,
)
_RELEASE = re.compile(r"放货|\b(?:release (?:the )?(?:cargo|goods)|telex release)\b", re.I)
_RELEASE_RULE = re.compile(
    r"才能|才可|方可|不得|到账|收到|后|之前|\b(?:only|after|until|unless|required)\b", re.I
)
_BATTERY = re.compile(
    r"无电池|不带电|不含电池|带电|含电池|(?:有|内置|配有).{0,14}电池|"
    r"\b(?:contains?|includes?|without|no|built-in|doesn['’´]t have|does not have)\b"
    r".{0,22}\bbatter(?:y|ies)\b",
    re.I,
)
_SPLIT = re.compile(r"分批|拆[两二三\d]+(?:批)?|\bpartial shipments?\b", re.I)
_SPLIT_RULE = re.compile(
    r"可以|可分|允许|不得|不分|不允许|需要|预计|必须|禁止|\b(?:allowed|permitted|required|not)\b",
    re.I,
)
_TRANSPORT_CONSTRAINT = re.compile(
    r"(?:general cargo\s*[-–:]\s*)?(?:non[- ]?)?stackable[.!。]?|"
    r"(?:cnee|consignee|customer)\s+(?:requests?|requested|requires?|required)\s+direct service[.!。]?",
    re.I,
)
RESPONSIBILITY = re.compile(
    r"(?=.*\b(?:origin|destination|local|pickup|delivery|airport|terminal|cargo)\b)"
    r"(?=.*\b(?:charges?|delivery|deliver|handling|freight)\b)"
    r"(?=.*\b(?:(?:by|for|on)\s+(?:the\s+)?(?:shipper|consignee|buyer|seller)(?:'s)?\s+account|"
    r"(?:paid|borne|covered) by (?:the )?(?:shipper|consignee|buyer|seller))\b).{10,220}",
    re.I,
)
_RESPONSIBILITY_TEMPLATE = re.compile(
    r"quote based on|rates? based on|报价适用|quote is valid|standard trading conditions|"
    r"\b(?:tentative|alternative|previous|additional)\b|备选|原先|加单|预计|未确认",
    re.I,
)
_BOILERPLATE = re.compile(
    r"请问|是否|务必告知|请务必|报价适用|以下报价|询价适用|如过安检|"
    r"\b(?:please advise|quote applies|please let us know)\b|[?？]",
    re.I,
)
_SHIPMENT_TERM = re.compile(
    r"[ \t]*(?:the shipper (?:informs|confirms) that )?"
    r"(?P<proof>this shipment is (?P<term>FCA|EXW) (?:at )?(?:their|our|the) warehouse)"
    r"(?:[ \t]*,[ \t]*kindly note there will be (?:a )?truck fee"
    r"(?: from (?:their|our|the) warehouse to [A-Z]{3})?[ \t]*:[ \t]*"
    r"(?:USD|EUR|CNY|RMB)[ \t]*\d+(?:\.\d{1,2})?)?[ \t]*[.。]?[ \t]*",
    re.I,
)
_RELATIVE_READY = re.compile(
    r"(?:[1-9]\d*[.、)]\s*)?货(?:物)?(?:将于|将在|会在|在)?"
    r"(?P<value>(?:今天|明天|后天|今日|明日)(?:早上|上午|中午|下午|晚上)?(?:之前|前)?)"
    r"(?:货好|备好|好|可提货|准备就绪)(?:\s*[,，；;]\s*(?:但(?:是)?|不过)\s*(?P<condition>.+))?[.。]?",
)
_RELATIVE_READY_EN = re.compile(
    r"(?:the )?cargo (?:will be|is) ready (?P<value>(?:by )?(?:today|tomorrow|the day after tomorrow)"
    r"(?: morning| afternoon| evening)?)[.!]?",
    re.I,
)
_SINGLE_GIVEN_NAME = re.compile(r"[A-ZÀ-ÖØ-Þ][^\W\d_]{1,29}")
_LABELED_EMAIL = re.compile(rf"(?:e[ -]?mail|mail|邮箱)\s*[:：]\s*(?P<v>{_EMAIL})", re.I)
_PANEL_PHONE = re.compile(
    r"(?:tel(?:ephone)?\.?|phone|mobile|联系电话|电话|手机)[ \t]*[:：][ \t]*"
    r"(?P<v>\+?[0-9 ()\-.]{7,28}(?:[ \t]*(?:ext\.?|x|分机|转|\*)[ \t]*\d{1,6})?)",
    re.I,
)
_CONTACT_SEPARATOR = re.compile(r"(?:add(?:ress)?|fax|地址|传真)\s*[:：].+", re.I)
_AGENT_LABELS = {
    "customer_company": re.compile(
        r"(?:customer company(?: name)?|客户公司(?:名|名称)?)\s*[:：]\s*(?P<v>[^@:/：]+)", re.I
    ),
    "contact_name": re.compile(
        r"(?:contact (?:person|name)|联系人(?:姓名)?)\s*[:：]\s*(?P<v>[^@:/：]+)", re.I
    ),
}
_CURRENT_CARGO_KIND = re.compile(
    r"(?:本票(?:货物)?(?:为|是)|本次货物(?:为|是)|this shipment (?:is|contains)\s+)"
    r"(?P<kind>普货|普通货物|general cargo|危险品|dangerous goods)[.。]?",
    re.I,
)
_READY_LABEL = re.compile(
    r"(?:cargo ready(?: date| time)?|ready date|货好(?:时间|日期))\s*[:：]", re.I
)
_RELATIVE_DATE = re.compile(
    r"(?:今天|明天|后天|今日|明日)(?:早上|上午|中午|下午|晚上)?|"
    r"(?:today|tomorrow|the day after tomorrow)(?: morning| afternoon| evening)?",
    re.I,
)
_SPLIT_LABEL = re.compile(
    r"(?:partial shipments?|split shipments?|是否允许分批|分批出货)\s*[:：]", re.I
)
_SPLIT_VALUE = re.compile(r"allowed|not allowed|permitted|not permitted|允许|不允许|禁止", re.I)


def declared_shipment_terms(text, current):
    """Bind only an explicit warehouse term from a verified current message line."""
    for line in re.finditer(r"[^\r\n]+", text):
        if (match := _SHIPMENT_TERM.fullmatch(line[0])) and current(line[0].strip()):
            start, end = match.span("proof")
            yield line.start() + start, line.start() + end, match["term"].upper()


def _valid_date(text):
    try:
        if match := re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text):
            date(*map(int, match.groups()))
            return True
        if match := _NAMED_DAY.fullmatch(text):
            date(int(match[3]) if match[3] else 2000, _MONTHS[match[2][:3].lower()], int(match[1]))
            return True
        day, month, *year = map(int, text.split("/"))
        for d, m in ((day, month), (month, day)):
            try:
                date(year[0] if year else 2000, m, d)
                return True
            except ValueError:
                continue
    except ValueError:
        return False
    return False


def _checked_lines(scoped_blocks, verify):
    lines = []
    for block, end in scoped_blocks:
        if block.get("cell"):
            continue  # Table labels use the main channel's separate table binding.
        for match in re.finditer(r"[^\r\n]+", block["text"][:end]):
            text = match.group().strip()
            if text:
                start = match.start() + len(match.group()) - len(match.group().lstrip())
                lines.append((text, (block, start, start + len(text)), verify(text)))
    return lines


def agent_body_detail_proofs(scoped_blocks, original):
    """Prove field-local alternatives without requiring a literal signoff or an absolute date."""
    verify = current_body_text_check(original, allow_quoted_duplicate=True)
    lines = _checked_lines(scoped_blocks, verify)
    values = {}

    def add(field, value, proof):
        values.setdefault(field, {}).setdefault(value, proof)

    phones, emails = [], []
    for i, (text, loc, verified) in enumerate(lines):
        if not verified:
            continue
        if i + 1 < len(lines):
            following, next_loc, next_verified = lines[i + 1]
            if next_verified and verify(text + " " + following):
                value = following.rstrip(".。")
                if _READY_LABEL.fullmatch(text) and (
                    _RELATIVE_DATE.fullmatch(value)
                    or (re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", value) and _valid_date(value))
                ):
                    add("cargo_ready_time", value, [next_loc, loc])
                if _SPLIT_LABEL.fullmatch(text) and _SPLIT_VALUE.fullmatch(value):
                    # Keep the exact label and polarity in the accepted remark.
                    add("business_remarks", text + " " + value, [next_loc, loc])
        if (
            match := _RELATIVE_READY.fullmatch(text) or _RELATIVE_READY_EN.fullmatch(text)
        ) and not _BOILERPLATE.search(text):
            condition = match.groupdict().get("condition")
            if condition is None or (
                _RELEASE.search(condition)
                and _RELEASE_RULE.search(condition)
                and not re.search(r"才能(?:货好|备好)|才(?:能|会)?(?:货好|备好|可提)", condition)
            ):
                add("cargo_ready_time", match["value"], [loc])
        if match := _CURRENT_CARGO_KIND.fullmatch(text):
            kind = match["kind"].casefold()
            add(
                "cargo_type",
                "general cargo"
                if kind in {"普货", "普通货物", "general cargo"}
                else "dangerous goods",
                [loc],
            )
        for field, pattern in _AGENT_LABELS.items():
            if (match := pattern.fullmatch(text)) and 2 <= len(value := match["v"].strip()) <= 120:
                if field != "contact_name" or (
                    len(value) <= 60
                    and (_NAME.fullmatch(value) or _SINGLE_GIVEN_NAME.fullmatch(value))
                    and not _NAME_BLOCKLIST.search(value)
                ):
                    add(field, value, [loc])
        if (match := _PANEL_PHONE.fullmatch(text)) and 7 <= sum(
            c.isdigit() for c in match["v"]
        ) <= 16:
            phones.append((i, match["v"].strip(), loc))
        if match := _LABELED_EMAIL.fullmatch(text):
            emails.append((i, match["v"], loc))
    if len(phones) == len(emails) == 1:
        (p, phone, phone_loc), (e, email, email_loc) = phones[0], emails[0]
        lo, hi = sorted((p, e))
        panel = lines[lo : hi + 1]
        if (
            hi - lo <= 3
            and all(ok for _, _, ok in panel)
            and all(_CONTACT_SEPARATOR.fullmatch(text) for text, _, _ in panel[1:-1])
            and verify(" ".join(text for text, _, _ in panel))
        ):
            add("contact_phone", phone, [phone_loc, email_loc])
            add("signature_email", email, [email_loc, phone_loc])
    return {field: next(iter(found.items())) for field, found in values.items() if len(found) == 1}


def inquiry_body_details(scoped_blocks, original):
    """Return unique field values with exact READ spans, only before the quote boundary."""
    verify = current_body_text_check(original, allow_quoted_duplicate=True)
    lines = _checked_lines(scoped_blocks, verify)
    observations = {}
    for text, loc, verified in lines:
        if not verified:
            continue
        if (
            (ready := _READY.search(text))
            and not _UNCERTAIN_DATE.search(text)
            and _valid_date(ready["value"])
        ):
            observations.setdefault("cargo_ready_time", {}).setdefault(ready["value"], [loc])
        if not _BOILERPLATE.search(text) and (
            (_RELEASE.search(text) and _RELEASE_RULE.search(text))
            or _BATTERY.search(text)
            or (_SPLIT.search(text) and _SPLIT_RULE.search(text))
            or _TRANSPORT_CONSTRAINT.fullmatch(text)
            or (RESPONSIBILITY.fullmatch(text) and not _RESPONSIBILITY_TEMPLATE.search(text))
        ):
            observations.setdefault("business_remarks", {}).setdefault(text, [loc])

    signoffs = [
        i for i, (text, _, checked) in enumerate(lines) if checked and _SIGNOFF.fullmatch(text)
    ]
    if len(signoffs) == 1:
        i = signoffs[0]
        marker = lines[i][1]
        signature = lines[i + 1 : i + 9]
        if any(
            ok
            and (
                _SIGN_EMAIL.fullmatch(text)
                or _SIGN_PHONE.fullmatch(text)
                or _MOBILE_OFFICE.match(text)
                or _COMPANY.search(text)
                or _ROLE_COMPANY.fullmatch(text)
            )
            for text, _, ok in signature
        ):
            for j, (text, loc, ok) in enumerate(signature):
                if not ok:
                    continue
                for field, pattern in (
                    ("signature_email", _SIGN_EMAIL),
                    ("contact_phone", _SIGN_PHONE),
                ):
                    if match := pattern.fullmatch(text) or (
                        field == "contact_phone" and _MOBILE_OFFICE.match(text)
                    ):
                        val = match["v"].strip()
                        if (
                            field == "contact_phone"
                            and not 7 <= sum(c.isdigit() for c in val) <= 16
                        ):
                            continue
                        observations.setdefault(field, {}).setdefault(val, [loc, marker])
                if company := _ROLE_COMPANY.fullmatch(text):
                    observations.setdefault("customer_company", {}).setdefault(
                        company["v"].strip(), [loc, marker]
                    )
                elif (
                    len(text) <= 120
                    and _COMPANY.search(text)
                    and not re.search(r"[@:/：]|^\d", text)
                ):
                    observations.setdefault("customer_company", {}).setdefault(text, [loc, marker])
                elif j == 0 and _NAME.fullmatch(text) and not _NAME_BLOCKLIST.search(text):
                    observations.setdefault("contact_name", {}).setdefault(text, [loc, marker])

    result = {}
    for field, values in observations.items():
        if field == "business_remarks":
            result[field] = ("\n".join(values), [locs[0] for locs in values.values()])
        elif len(values) == 1:
            result[field] = next(iter(values.items()))
    return result

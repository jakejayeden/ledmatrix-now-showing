"""Today's showtimes from Savoy Cinema Nottingham for LEDMatrix."""

from datetime import datetime
from html.parser import HTMLParser
import re
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont

from src.plugin_system.base_plugin import BasePlugin


BOOKING_URL = "https://savoyonline.co.uk/SavoyNottingham.dll/Booking"
UK_ZONE = ZoneInfo("Europe/London")
TIME_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*"
    r"(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+(\d{4})",
    re.IGNORECASE,
)
BLOCK_TAGS = {"article", "div", "li", "p", "section", "tr", "ul", "ol"}
IGNORED_HEADINGS = {"book tickets", "welcome", "menu", "my order"}
RATING_SUFFIX_RE = re.compile(r"\s+\((?:U|PG|12A|12|15|18|TBC)(?:\s+LIVE)?\)\s*$", re.IGNORECASE)
MICRO_FONT = {
    "A": ("010", "101", "111", "101", "101"), "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"), "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"), "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"), "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"), "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "101", "110", "101", "101"), "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"), "N": ("101", "111", "111", "111", "101"),
    "O": ("010", "101", "101", "101", "010"), "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "111", "011"), "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"), "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"), "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"), "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"), "Z": ("111", "001", "010", "100", "111"),
    "0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"), "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "110", "101", "010"), "7": ("111", "001", "010", "010", "010"),
    "8": ("010", "101", "010", "101", "010"), "9": ("010", "101", "011", "001", "110"),
    "@": ("011", "100", "111", "101", "011"), "-": ("000", "000", "111", "000", "000"),
    ":": ("000", "010", "000", "010", "000"), ".": ("000", "000", "000", "000", "010"),
    "'": ("010", "010", "000", "000", "000"), "!": ("010", "010", "010", "000", "010"),
    " ": ("000", "000", "000", "000", "000"),
}


class _ListingText(HTMLParser):
    """Turn the booking page into heading and text lines without extra deps."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self.line = []
        self.heading = []
        self.heading_depth = 0

    def _flush_line(self):
        value = " ".join(" ".join(self.line).split())
        if value:
            self.events.append(("text", value))
        self.line = []

    def handle_starttag(self, tag, attrs):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush_line()
            self.heading_depth += 1
        elif tag == "br":
            if not self.heading_depth:
                self._flush_line()
        elif tag in BLOCK_TAGS and not self.heading_depth:
            self._flush_line()

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading_depth:
            self.heading_depth -= 1
            if not self.heading_depth:
                value = " ".join(" ".join(self.heading).split())
                if value:
                    self.events.append(("heading", value))
                self.heading = []
        elif tag in BLOCK_TAGS and not self.heading_depth:
            self._flush_line()

    def handle_data(self, data):
        if self.heading_depth:
            self.heading.append(data)
        else:
            self.line.append(data)

    def result(self):
        self._flush_line()
        return self.events


def _parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    day, month, year = match.groups()
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(f"{day} {month} {year}", fmt).date()
        except ValueError:
            pass
    return None


def parse_today_listings(html, today):
    """Extract film titles and today's times from Savoy's public booking page."""
    parser = _ListingText()
    parser.feed(html)

    films = []
    current_film = None
    current_date = None
    events = parser.result()
    for event_index, (kind, text) in enumerate(events):
        if kind == "heading":
            candidate = text.strip()
            heading_date = _parse_date(candidate)
            if heading_date:
                current_date = heading_date
                continue
            if candidate.casefold() in IGNORED_HEADINGS:
                continue

            # A real film card is followed by its BOOK TICKETS heading. Other
            # page headings (navigation, events, and footer cards) are not film
            # titles, even if they happen to contain text that looks like one.
            next_heading = next(
                (value.strip().casefold() for event_kind, value in events[event_index + 1:]
                 if event_kind == "heading"),
                "",
            )
            if next_heading != "book tickets":
                current_film = None
                current_date = None
                continue

            candidate = RATING_SUFFIX_RE.sub("", candidate).rstrip(" .")
            if candidate and len(candidate) > 2:
                current_film = candidate
                current_date = None
            continue

        found_date = _parse_date(text)
        if found_date:
            current_date = found_date
        if current_film and current_date == today:
            if "sold out" in text.casefold():
                continue
            times = TIME_RE.findall(text)
            if times:
                record = next((film for film in films if film["title"] == current_film), None)
                if record is None:
                    record = {"title": current_film, "times": []}
                    films.append(record)
                record["times"].extend(times)

    for film in films:
        film["times"] = sorted(set(film["times"]))
        film["title"] = normalize_title(condense_title(film["title"]))
    return [film for film in films if film["times"]]


def normalize_title(text):
    """Use clean, stable ASCII for the matrix font; keep the whole title."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return " ".join(text.upper().split())


def condense_title(text):
    """Drop cinema-programme prefixes that bury the actual film title."""
    prefixes = (
        "Nottingham Film Festival Opening Night - ",
        "Nottingham Film Festival First Time Filmmakers & Student Shorts - ",
        "Nottingham Film Festival Experimental/Horror - ",
        "RBO 2026-27 Cinema Season: ",
    )
    for prefix in prefixes:
        if text.casefold().startswith(prefix.casefold()):
            return text[len(prefix):]
    substitutions = (
        ("National Theatre Live: ", "NT Live: "),
        ("The Metropolitan Opera: ", "Met Opera: "),
        ("Opéra De Paris Ballet - ", "Paris Ballet: "),
        ("Andrew Lloyd Webber’s Starlight Express - Live On Stage", "Starlight Express: Live On Stage"),
        ("Andrew Lloyd Webber's Starlight Express - Live On Stage", "Starlight Express: Live On Stage"),
    )
    for prefix, replacement in substitutions:
        if text.casefold().startswith(prefix.casefold()):
            return replacement + text[len(prefix):]
    return text


class NowShowingCinemaPlugin(BasePlugin):
    """Fetch Savoy Nottingham listings and render a compact 32px ticker."""

    def __init__(self, plugin_id, config, display_manager, cache_manager, plugin_manager):
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)
        self._load_config(self.config)
        self.films = []
        self.last_fetch = 0
        self.last_error = None
        self.page_index = 0
        self.detail_index = 0
        self.last_page_change = 0
        self.last_day = None

    def _load_config(self, config):
        self.savoy_nottingham = config.get("savoy_nottingham", True)
        self.refresh_minutes = max(5, min(180, int(config.get("refresh_minutes", 30))))
        self.display_duration = max(5, min(120, int(config.get("display_duration", 10))))

    def on_config_change(self, new_config):
        super().on_config_change(new_config)
        self._load_config(self.config)

    def _now(self):
        return datetime.now(UK_ZONE)

    def update(self):
        now = self._now()
        today = now.date()
        day_changed = today != self.last_day
        expired = time.time() - self.last_fetch >= self.refresh_minutes * 60
        if not self.savoy_nottingham:
            self.films = []
            self.last_error = None
            self.last_day = today
            return
        if not (day_changed or expired):
            return

        request = Request(
            BOOKING_URL,
            headers={"User-Agent": "LEDMatrix-NowShowing/0.1 (public cinema listings display)"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(charset, errors="replace")
            self.films = parse_today_listings(html, today)
            self.last_error = None
            self.last_day = today
            self.last_fetch = time.time()
            self.page_index = 0
            self.detail_index = 0
            self.logger.info("Loaded %d Savoy Nottingham film listings", len(self.films))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            self.last_error = str(exc)
            self.last_fetch = time.time()
            self.last_day = today
            self.logger.warning("Could not refresh Savoy Nottingham listings: %s", exc)

    def display(self, force_clear=False):
        display = self.display_manager
        width = max(1, int(display.width))
        height = max(1, int(display.height))
        now = self._now()
        font = self._display_font()

        # Match NCT Bus Departures: its 9px Pillow default font and a fresh
        # black image are also used here. Redrawing onto the old canvas left
        # pixels from the previous title behind the new title.
        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        date_text = now.strftime("%d%b").upper() if width < 180 else now.strftime("%a %d %b").upper()
        date_width = draw.textbbox((0, 0), date_text, font=font)[2]
        date_x = max(1, width - date_width - 1)
        header = self._fit_text(draw, "NOW SHOWING @ SAVOY", font, max(1, date_x - 3))
        draw.text((1, -1), header, font=font, fill=(255, 255, 255))
        draw.text((date_x, -1), date_text, font=font, fill=(255, 255, 255))
        draw.line((0, min(10, height - 1), width - 1, min(10, height - 1)), fill=(255, 0, 0))

        if not self.savoy_nottingham:
            title, times = "SAVOY NOTTINGHAM", "DISABLED"
        elif not self.films:
            title, times = ("NO SCREENINGS", "" if not self.last_error else "CHECK CONNECTION")
        else:
            film = self.films[self.page_index % len(self.films)]
            title, times = film["title"], " ".join(film["times"])

        title = self._fit_text(draw, title, font, width - 2)
        time_pages = self._split_to_width(draw, times, font, width - 2) if times else [""]
        detail = self.detail_index % len(time_pages)
        draw.text((1, 12), title, font=font, fill=(255, 0, 0))
        if times:
            draw.text((1, 22), time_pages[detail], font=font, fill=(255, 255, 255))

        if self.films and time.time() - self.last_page_change >= self.display_duration:
            if self.detail_index + 1 >= len(time_pages):
                self.detail_index = 0
                self.page_index = (self.page_index + 1) % len(self.films)
            else:
                self.detail_index += 1
            self.last_page_change = time.time()
        display.image = image
        display.update_display()

    @staticmethod
    def _display_font():
        """Use the same Pillow default font and size as NCT Bus Departures."""
        try:
            return ImageFont.load_default(size=9)
        except TypeError:
            return ImageFont.load_default()

    @staticmethod
    def _fit_text(draw, text, font, max_width):
        text = str(text)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return text
        while text and draw.textbbox((0, 0), text + "..", font=font)[2] > max_width:
            text = text[:-1]
        return text + ".."

    @classmethod
    def _split_to_width(cls, draw, text, font, max_width):
        """Keep every showtime visible, paging only if the line is too long."""
        if not text:
            return [""]
        pages = []
        current = ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                pages.append(current)
                current = word
            else:
                current = candidate
        if current:
            pages.append(current)
        return pages or [""]

    def get_info(self):
        info = super().get_info()
        info.update({
            "cinema": "Savoy Nottingham",
            "film_count": len(self.films),
            "last_error": self.last_error,
        })
        return info

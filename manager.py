"""Today's showtimes from Savoy Cinema Nottingham for LEDMatrix."""

from datetime import datetime
from html.parser import HTMLParser
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

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
    for kind, text in parser.result():
        if kind == "heading":
            candidate = text.strip()
            heading_date = _parse_date(candidate)
            if heading_date:
                current_date = heading_date
                continue
            if candidate.casefold() not in IGNORED_HEADINGS and not _parse_date(candidate):
                # Booking page film cards use headings; generic heading text is
                # ignored by the title and rating suffix cleanup below.
                candidate = re.sub(r"\s+\([^()]{1,8}\)\s*$", "", candidate).strip()
                if candidate and len(candidate) > 2:
                    current_film = candidate
                    current_date = None
            continue

        found_date = _parse_date(text)
        if found_date:
            current_date = found_date
        if current_film and current_date == today:
            times = TIME_RE.findall(text)
            if times:
                record = next((film for film in films if film["title"] == current_film), None)
                if record is None:
                    record = {"title": current_film, "times": []}
                    films.append(record)
                record["times"].extend(times)

    for film in films:
        film["times"] = sorted(set(film["times"]))
    return [film for film in films if film["times"]]


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
        self.display_duration = max(5, min(120, int(config.get("display_duration", 15))))

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
            self.logger.info("Loaded %d Savoy Nottingham film listings", len(self.films))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            self.last_error = str(exc)
            self.last_fetch = time.time()
            self.last_day = today
            self.logger.warning("Could not refresh Savoy Nottingham listings: %s", exc)

    def display(self, force_clear=False):
        display = self.display_manager
        width = display.width
        height = display.height
        now = self._now()
        if force_clear:
            display.clear()

        # The bundled bitmap font is intentionally used so all labels remain
        # legible at the 128x32 target; the same coordinates adapt to 256x32.
        title = "NOW SHOWING"
        date_text = now.strftime("%d %b").upper() if width < 180 else now.strftime("%a %d %b %Y").upper()
        display.draw_text(title, x=1, y=0, color=(255, 255, 255))
        date_width = display.get_text_width(date_text, display.regular_font)
        display.draw_text(date_text, x=max(1, width - date_width - 1), y=0, color=(255, 255, 255))
        display.draw.line((0, 9, width - 1, 9), fill=(255, 0, 0))

        if not self.savoy_nottingham:
            lines = [("Savoy Nottingham", "disabled")]
        elif not self.films:
            lines = [("No screenings today", "" if not self.last_error else "Check connection")]
        else:
            film = self.films[self.page_index % len(self.films)]
            lines = [(film["title"], "  ".join(film["times"]))]

        film_title, times = lines[0]
        film_pages = self._split_to_width(display, film_title, width - 2)
        time_pages = self._split_to_width(display, times, width - 2) if times else [""]
        page_count = max(len(film_pages), len(time_pages))
        detail = self.detail_index % page_count
        display.draw_text(film_pages[min(detail, len(film_pages) - 1)], x=1, y=12, color=(255, 0, 0))
        if times:
            display.draw_text(time_pages[min(detail, len(time_pages) - 1)], x=1, y=22, color=(255, 255, 255))

        if self.films and time.time() - self.last_page_change >= self.display_duration:
            self.detail_index += 1
            if self.detail_index >= page_count:
                self.detail_index = 0
                self.page_index = (self.page_index + 1) % len(self.films)
            self.last_page_change = time.time()
        display.update_display()

    @staticmethod
    def _split_to_width(display, text, max_width):
        """Break long names or time lists into readable panel-width pages."""
        if not text:
            return [""]
        font = display.regular_font
        words = text.split()
        pages = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and display.get_text_width(candidate, font) > max_width:
                pages.append(current)
                current = word
            else:
                current = candidate
        if current:
            pages.append(current)
        # A single unusually long token (rare for a title or a time) still
        # needs splitting so its tail is not silently clipped.
        fitted = []
        for page in pages:
            part = ""
            for char in page:
                candidate = part + char
                if part and display.get_text_width(candidate, font) > max_width:
                    fitted.append(part)
                    part = char
                else:
                    part = candidate
            if part:
                fitted.append(part)
        return fitted or [""]

    def get_info(self):
        info = super().get_info()
        info.update({
            "cinema": "Savoy Nottingham",
            "film_count": len(self.films),
            "last_error": self.last_error,
        })
        return info

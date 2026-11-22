# Now Showing Cinema

An LEDMatrix plugin for today's cinema film listings. The first supported venue
is Savoy Cinema Nottingham. It reads the cinema's public Booking page, selects
screenings for today's date in the `Europe/London` timezone, and cycles through
the films so their showtimes can be read on a 32-pixel-high display.

## Display

- `NOW SHOWING` at the upper left and today's date at the upper right
- A thin red divider
- The film title in red and today's available showtimes below it
- One film per page, rotating through today's programme
- Long titles or time lists are split across consecutive pages to fit the panel

The same layout runs at 128x32 and 256x32. At 128 pixels wide the date is
compact (`22 NOV`); wider panels show the weekday and year.

## Install

In LEDMatrix, open **Plugin Manager  Install from GitHub** and enter:

```text
https://github.com/jakejayeden/ledmatrix-now-showing
```

Enable **Savoy Nottingham** in the plugin settings. The plugin uses only the
Python standard library and needs an internet connection to refresh listings.

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| Savoy Nottingham | enabled | Include the Nottingham venue |
| Refresh interval | 30 minutes | How often to fetch the public booking page |
| Display duration | 15 seconds | How long to show each film before moving on |

## Data source

The plugin uses Savoy Nottingham's public booking page:
<https://savoyonline.co.uk/SavoyNottingham.dll/Booking>

The site does not currently expose a documented public API. The parser reads
the date-labelled film and showtime content from the page and ignores future
dates. If Savoy changes its page structure, the parser may need an update.

## Configuration example

See [`example_config.json`](example_config.json).


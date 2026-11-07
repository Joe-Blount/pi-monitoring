"""What the forecast service answers, built by hand and never fetched.

The layout follows the service's documented answer to the request the driver
makes: hourly stamps and radiation side by side, one sunrise per day, all in
the local time of the zone that was asked for. The position in it is invented,
as is the one the tests hand the driver, and both have to be: the answer echoes
a position back, and nothing the driver publishes may carry it.
"""

import json
import zoneinfo

ZONE = zoneinfo.ZoneInfo("America/Chicago")

#: A position nowhere near any site, precise enough to show the rounding.
PLACE = {"latitude": 12.34, "longitude": -56.78, "zone": ZONE}

FIRST_SUNRISE = "2026-11-07T06:58"
SECOND_SUNRISE = "2026-11-08T07:00"


def answer(first=(100.0, 200.0, 300.0, 400.0), second=(10.0, 20.0, 30.0, 40.0),
           sunrises=(FIRST_SUNRISE, SECOND_SUNRISE)):
    """Two days of radiation, zero but for the hours after each sunrise.

    `first` lands on 08:00 to 11:00 of the first day and `second` on 08:00 to
    11:00 of the second, which are the four hours after each of those sunrises
    given that each stamp carries the mean of the hour before it.
    """
    times, values = [], []
    for day, morning in (("2026-11-07", first), ("2026-11-08", second)):
        for hour in range(24):
            times.append("%sT%02d:00" % (day, hour))
            offset = hour - 8
            values.append(morning[offset] if 0 <= offset < len(morning)
                          else 0.0)
    return {
        "latitude": 12.375,
        "longitude": -56.75,
        "utc_offset_seconds": -21600,
        "timezone": "America/Chicago",
        "hourly_units": {"time": "iso8601", "shortwave_radiation": "W/m2"},
        "hourly": {"time": times, "shortwave_radiation": values},
        "daily_units": {"time": "iso8601", "sunrise": "iso8601"},
        "daily": {"time": ["2026-11-07", "2026-11-08"],
                  "sunrise": list(sunrises)},
    }


def body(document=None):
    return json.dumps(answer() if document is None else document).encode()

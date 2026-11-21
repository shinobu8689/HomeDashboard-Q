import requests
from datetime import datetime, timedelta
import os
import time



# 🔧 CHANGE THESE to your location
LATITUDE = -37.8136   # Melbourne latitude
LONGITUDE = 144.9631  # Melbourne longitude
LOCATION = "Melbourne"

url = "https://api.open-meteo.com/v1/forecast"

params = {
    "latitude": LATITUDE,
    "longitude": LONGITUDE,
    "current_weather": True,
    "hourly": "temperature_2m,relative_humidity_2m,precipitation_probability,weathercode",
    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    "timezone": "auto"
}

weather_codes = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Depositing rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Moderate drizzle", "🌦️"),
    55: ("Dense drizzle", "🌧️"),
    56: ("Freezing drizzle: light", "🌧️❄️"),
    57: ("Freezing drizzle: dense", "🌧️❄️"),
    61: ("Light rain", "🌧️"),
    63: ("Moderate rain", "🌧️"),
    65: ("Heavy rain", "🌧️🌧️"),
    66: ("Freezing rain: light", "🌧️❄️"),
    67: ("Freezing rain: heavy", "🌧️❄️"),
    71: ("Light snow", "🌨️"),
    73: ("Moderate snow", "🌨️"),
    75: ("Heavy snow", "🌨️❄️"),
    77: ("Snow grains", "❄️"),
    80: ("Rain showers: slight", "🌦️"),
    81: ("Rain showers: moderate", "🌧️"),
    82: ("Rain showers: heavy", "🌧️🌧️"),
    85: ("Snow showers slight", "🌨️"),
    86: ("Snow showers heavy", "🌨️❄️"),
    95: ("Thunderstorm: slight or moderate", "⛈️"),
    96: ("Thunderstorm with slight hail", "⛈️🌨️"),
    99: ("Thunderstorm with heavy hail", "⛈️🌨️")
}

def rain_bar_3(p):
    """Return 3-block rain visual based on precipitation %"""
    if p == 0:
        return "▒▒▒"
    elif p <= 33:
        return "▒▒▓"
    elif p <= 66:
        return "▒▓▓"
    else:  # 67-100
        return "▓▓▓"

def weather_display():
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    # --- Current weather status ---
    current_code = data["current_weather"]["weathercode"]
    condition_text, condition_emoji = weather_codes.get(current_code, ("Unknown", "❓"))

    # --- Current weather & humidity ---
    current_temp = round(data["current_weather"]["temperature"])
    current_humidity = data["hourly"]["relative_humidity_2m"][0]
    current_precip = data["hourly"]["precipitation_probability"][0]

    # --- Today --- (daily min/max)
    today_min = round(data["daily"]["temperature_2m_min"][0])
    today_max = round(data["daily"]["temperature_2m_max"][0])

    now = datetime.now()
    text = f"🔄 {now.strftime("%H:%M")}"
    width = 32

    print()
    print(f"📍 {LOCATION}\t🌡  {current_temp}° 🌧️ {current_precip}% {text:>{width}}")
    print(f"{condition_emoji} {condition_text}")
    print(f"🔼 {today_max}°  🔽️ {today_min}°  💧 {current_humidity}%")

    # --- Upcoming 7 hours forecast ---
    if True:
        print()
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        precs = data["hourly"]["precipitation_probability"]
        codes = data["hourly"]["weathercode"]

        # Find index of next hour
        current_time = datetime.fromisoformat(data["current_weather"]["time"])
        next_hour_time_str = (current_time + timedelta(hours=1)).isoformat(timespec='minutes')

        # Find the closest index that is >= next_hour_time_str
        idx = 0
        for i, t in enumerate(times):
            if t >= next_hour_time_str:
                idx = i
                break

        # Slice the next 7 hours
        hourly_times = [datetime.fromisoformat(t).strftime("%H:%M") for t in times[idx:idx + 7]]
        hourly_emojis = [weather_codes.get(c, ("Unknown", "❓"))[1] for c in codes[idx:idx + 7]]
        hourly_temps = [f"{emoji} {round(t)}°" for emoji, t in zip(hourly_emojis, temps[idx:idx + 7])]
        hourly_precs = [f"   {round(p)}%" for p in precs[idx:idx + 7]]


        # Print horizontally
        print("🕒\t" + "\t".join(hourly_times))
        print("\t" + "\t".join(hourly_temps))
        print("🌧️\t" + "\t".join(hourly_precs))

    print()

    # --- 7-day forecast ---
    dates = data["daily"]["time"]
    tmax = data["daily"]["temperature_2m_max"]
    tmin = data["daily"]["temperature_2m_min"]
    prec_daily = data["daily"]["precipitation_probability_max"]
    prec_daily_fmt = [f"{round(p)}%" for p in prec_daily]


    # Format dates as dd/mm
    dates_fmt = [
        datetime.fromisoformat(d).strftime("%d/%m")
        for d in dates
    ]

    # Horizontal output
    print("📆\t" + "\t".join(dates_fmt))
    print("🔼\t" + "\t".join(f"{t:.0f}°" for t in tmax))
    print("🔽️\t" + "\t".join(f"{t:.0f}°" for t in tmin))
    print("🌧️\t" + "\t".join(prec_daily_fmt))

def get_weather_text(): # weather_display() but return result instead of print
    lines = []

    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    current_code = data["current_weather"]["weathercode"]
    condition_text, condition_emoji = weather_codes.get(current_code, ("Unknown", "❓"))

    current_temp = round(data["current_weather"]["temperature"])
    current_humidity = data["hourly"]["relative_humidity_2m"][0]
    current_precip = data["hourly"]["precipitation_probability"][0]

    today_min = round(data["daily"]["temperature_2m_min"][0])
    today_max = round(data["daily"]["temperature_2m_max"][0])

    now = datetime.now().strftime("%H:%M")

    lines.append(f"📍 {LOCATION}  🌡 {current_temp}°  🌧️ {current_precip}%   🔄 {now}")
    lines.append(f"{condition_emoji} {condition_text}")
    lines.append(f"🔼 {today_max}°  🔽 {today_min}°  💧 {current_humidity}%")

    return "\n".join(lines)



if __name__ == "__main__":
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        weather_display()
        time.sleep(60*5)   # sec



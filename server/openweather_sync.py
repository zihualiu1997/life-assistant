"""OpenWeatherMap 3-hour forecast for the configured city and timezone."""
import datetime as dt
import gzip
import io
import json
import re
import urllib.request
from urllib.error import HTTPError
from life_assistant import ROOT, TZ, atomic_write, lock


class WeatherError(Exception):
    pass


def validate(key):
    if not re.fullmatch(r'[a-zA-Z0-9]{16,128}', key.strip()):
        raise WeatherError('请填写 OpenWeatherMap API Key。')
    return {'key': key.strip()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WeatherError('接口发生重定向，已停止请求。')


def fetch(credentials, latitude=0, longitude=0):
    from urllib.parse import urlencode
    credentials = validate(credentials['key'])
    query = urlencode({'lat': latitude, 'lon': longitude, 'appid': credentials['key'],
                       'units': 'metric', 'lang': 'zh_cn'})
    # Free-plan forecast endpoint only. Never print the URL (it contains the key).
    url = 'https://api.openweathermap.org/data/2.5/forecast?' + query
    req = urllib.request.Request(url, headers={
        'Accept-Encoding': 'gzip', 'User-Agent': 'LocalMorningReport/1.0'})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=25) as response:
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise WeatherError('天气响应异常过大。')
            if response.headers.get('Content-Encoding') == 'gzip':
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                    data = compressed.read(2_000_001)
                if len(data) > 2_000_000:
                    raise WeatherError('天气响应异常过大。')
            return json.loads(data)
    except HTTPError as exc:
        raise WeatherError(f'OpenWeatherMap返回 HTTP {exc.code}，请检查密钥激活状态、免费接口权限及账户额度。') from None


def summarize(raw, now, city='', latitude=0, longitude=0):
    if str(raw.get('cod')) != '200':
        raise WeatherError('天气接口未返回成功结果，请检查密钥与账户权限。')
    today = now.date().isoformat()
    periods = []
    for entry in raw.get('list', []):
        stamp = dt.datetime.fromtimestamp(entry['dt'], now.tzinfo)
        if stamp.date() != now.date() or stamp < now:
            continue
        periods.append({'time': stamp.isoformat(), 'temperature_c': entry['main']['temp'],
                        'description': entry['weather'][0]['description'],
                        'precipitation_probability': entry.get('pop'),
                        'rain_3h_mm': entry.get('rain', {}).get('3h'),
                        'wind_speed_ms': entry.get('wind', {}).get('speed')})
    if not periods:
        raise WeatherError('没有当地时间今天尚未过去的预报时段，不使用旧天气或明天天气代替。')
    periods.sort(key=lambda item: item['time'])
    return {'status': 'synced', 'date': today, 'fetched_at': now.isoformat(),
            'city': city, 'reference_location': {'latitude': latitude, 'longitude': longitude},
            'forecast_type': '3-hour forecast; remaining periods today, not whole-day extremes',
            'periods': periods, 'source': 'OpenWeatherMap',
            'source_url': 'https://openweathermap.org/'}


def sync(root=ROOT, credentials=None, force=False, *, location=None, timezone=None):
    folder = root / '.local' / 'openweather'
    now = dt.datetime.now(timezone or TZ)
    place = location or {'city': '', 'latitude': 0, 'longitude': 0}
    with lock(root, 'openweather'):
        cache = folder / 'latest.json'
        if cache.exists() and not force:
            old = json.loads(cache.read_text(encoding='utf-8'))
            if (old.get('status') == 'synced' and old.get('date') == now.date().isoformat()
                    and dt.datetime.fromisoformat(old['fetched_at']).astimezone(now.tzinfo).date() == now.date()
                    and (location is None or (old.get('city') == place['city'] and old.get('reference_location') == {k: place[k] for k in ('latitude', 'longitude')}))
                    and any(dt.datetime.fromisoformat(x['time']) >= now for x in old.get('periods', []))):
                return {'status': 'cached', 'path': str(cache)}
        try:
            if credentials is None:
                raise WeatherError('尚未配置 OpenWeatherMap，请在网页设置中填写 API Key。')
            raw = fetch(credentials) if location is None else fetch(credentials, place['latitude'], place['longitude'])
            result = summarize(raw, now, place['city'], place['latitude'], place['longitude'])
            atomic_write(folder / (now.date().isoformat() + '.json'), json.dumps(raw, ensure_ascii=False, indent=2))
            atomic_write(cache, json.dumps(result, ensure_ascii=False, indent=2))
            return {'status': 'synced', 'path': str(cache)}
        except Exception as exc:
            message = str(exc) if isinstance(exc, WeatherError) else '天气获取失败，请检查网络或配置；未使用旧预报。'
            atomic_write(cache, json.dumps({'status': 'unavailable', 'date': now.date().isoformat(),
                                           'checked_at': now.isoformat(), 'message': message}, ensure_ascii=False, indent=2))
            raise WeatherError(message) from None



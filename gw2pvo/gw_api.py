import base64
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, date as date_cls, timedelta
import requests

__author__ = "Mark Ruys"
__copyright__ = "Copyright 2017, Mark Ruys"
__license__ = "MIT"
__email__ = "mark@paracas.nl"

# Rewritten for SEMS+ (2026). The legacy SEMS API still accepts logins but
# returns empty data for migrated plants, so this version:
#   1. logs in via the SEMS+ web cross-login (with the headers the
#      semsplus.goodwe.com web app sends; without them: "parameter error"),
#   2. reads the per-minute system power curve from
#      sems-plant/api/v1/hems/power/statisticsAndPreV2,
#   3. if GW_INVERTER_SN is set, reads live power, grid/PV voltage and
#      temperature from sems-plant/api/equipments/<sn>/telemetry, and
#      today's/lifetime energy from .../<sn>/telecounting,
#   4. otherwise derives power and today's energy from the power curve.
# The curve is also used to detect an offline inverter and for --date.

LOGIN_URL = "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"
FALLBACK_API = "https://eu-gateway.semsportal.com/web/sems"
POWER_CURVE_PATH = "/sems-plant/api/v1/hems/power/statisticsAndPreV2"
TELEMETRY_PATH = "/sems-plant/api/equipments/{sn}/telemetry"
TELECOUNTING_PATH = "/sems-plant/api/equipments/{sn}/telecounting"
# Inverter serial number for live telemetry (power, voltages, temperature).
# Set it with --gw-inverter-sn / gw_inverter_sn in gw2pvo.cfg, or in the
# environment, e.g. GW_INVERTER_SN=YOUR_SERIAL (used as a fallback).
# Without it only the power curve is used.
INVERTER_SN_ENV = "GW_INVERTER_SN"
SUCCESS_CODES = ("0", "00000")

# Power values from the curve are multiplied by this to get watts.
POWER_SCALE_TO_W = 1
# A sample older than this means the inverter is not reporting.
STALE_AFTER = timedelta(minutes=15)
# Gaps in the curve longer than this are not integrated.
MAX_GAP = timedelta(minutes=15)

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "os": "Windows 10.0 desktop",
    "brand": "Chrome 155.0.0.0 Google Inc. en-US Europe/Amsterdam",
    "neutral": "0",
    "currentlang": "en",
    "Origin": "https://semsplus.goodwe.com",
    "Referer": "https://semsplus.goodwe.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
}
EMPTY_TOKEN = '{"uid":"","timestamp":0,"token":"","client":"semsPlusWeb","version":"","language":"en"}'


class GoodWeApi:

    def __init__(self, system_id, account, password, inverter_sn=None):
        self.inverter_sn = inverter_sn or os.environ.get(INVERTER_SN_ENV)
        self.system_id = system_id
        self.account = account
        self.password = password
        self.uuid = str(uuid.uuid4())
        self.token_data = None
        self.api = None

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _signature(uid="", token=""):
        ms = round(time.time() * 1000)
        digest = hashlib.sha256("{}@{}@{}".format(ms, uid, token).encode()).hexdigest()
        return base64.b64encode("{}@{}".format(digest, ms).encode()).decode()

    def _headers(self):
        headers = dict(BROWSER_HEADERS)
        if self.token_data:
            headers["uuid"] = self.token_data.get("uuid") or self.uuid
            headers["Token"] = json.dumps(self.token_data)
            headers["X-Signature"] = self._signature(
                self.token_data.get("uid", ""), self.token_data.get("token", ""))
        else:
            headers["uuid"] = self.uuid
            headers["Token"] = EMPTY_TOKEN
            headers["X-Signature"] = self._signature()
        return headers

    @staticmethod
    def _tz_param(day):
        ''' SEMS+ wants the offset like JS getTimezoneOffset()/60: UTC+2 -> -2. '''
        noon = datetime(day.year, day.month, day.day, 12).astimezone()
        return -int(noon.utcoffset().total_seconds() // 3600)

    # --------------------------------------------------------------- login

    def login(self):
        self.token_data = None
        md5 = hashlib.md5(self.password.encode("utf-8")).hexdigest()
        body = {
            "account": self.account,
            "pwd": base64.b64encode(md5.encode("utf-8")).decode("utf-8"),
            "agreement": 1,
            "isLocal": False,
            "isChinese": False,
        }
        r = requests.post(LOGIN_URL, headers=self._headers(), json=body, timeout=15)
        r.raise_for_status()
        resp = r.json()
        code = str(resp.get("code"))
        data = resp.get("data")
        if code not in SUCCESS_CODES or not isinstance(data, dict) or not data.get("token"):
            raise Exception("SEMS+ login failed: code={} {}".format(
                code, resp.get("description") or resp.get("msg")))
        token = dict(data)
        token.setdefault("uuid", self.uuid)
        api = resp.get("api") if isinstance(resp.get("api"), str) and resp.get("api") else data.get("api")
        self.api = (api or FALLBACK_API).rstrip("/")
        self.token_data = token
        logging.info("Logged in to GoodWe SEMS+, API %s", self.api)

    def call(self, path, payload=None, method="POST", params=None):
        relogged = False
        for i in range(1, 4):
            try:
                if self.token_data is None:
                    self.login()
                    relogged = True
                r = requests.request(method, self.api + path, headers=self._headers(),
                                     json=payload, params=params, timeout=15)
                if r.status_code in (401, 403) and not relogged:
                    self.token_data = None
                    continue
                r.raise_for_status()
                data = r.json()
                logging.debug(data)
                code = str(data.get("code"))
                if code in SUCCESS_CODES:
                    return data.get("data") or {}
                if not relogged:
                    # Most likely an expired session: log in once more and retry
                    logging.info("SEMS+ call returned code %s (%s), logging in again",
                                 code, data.get("description") or data.get("msg"))
                    self.token_data = None
                    continue
                raise Exception("Failed to call GoodWe API (code {}, {})".format(
                    code, data.get("description") or data.get("msg")))
            except requests.exceptions.RequestException as exp:
                logging.warning(exp)
            time.sleep(i ** 3)
        raise Exception("Failed to call GoodWe API (too many retries)")

    # ---------------------------------------------------------------- data

    def getPowerCurve(self, day):
        ''' List of (datetime, watts) for the given day, null samples dropped. '''
        payload = {
            "stationId": self.system_id,
            "items": ["pSystem"],
            "timeScale": 1,
            "timeZone": self._tz_param(day),
            "startTime": day.strftime("%Y-%m-%d 00:00:00"),
            "endTime": day.strftime("%Y-%m-%d 23:59:59"),
        }
        data = self.call(POWER_CURVE_PATH, payload)
        samples = []
        for series in data.get("dataList") or []:
            if series.get("item") != "pSystem":
                continue
            for point in series.get("powerData") or []:
                if point.get("power") is None:
                    continue
                try:
                    tp = datetime.strptime(point["tp"], "%Y-%m-%d %H:%M:%S")
                except (KeyError, ValueError):
                    continue
                samples.append((tp, float(point["power"]) * POWER_SCALE_TO_W))
        samples.sort()
        return samples

    @staticmethod
    def _cumulative_kwh(samples):
        ''' Trapezoidal integration; yields (dt, watts, kwh_so_far). '''
        kwh = 0.0
        prev = None
        for tp, w in samples:
            if prev is not None:
                gap = tp - prev[0]
                if timedelta(0) < gap <= MAX_GAP:
                    kwh += (prev[1] + w) / 2 / 1000 * gap.total_seconds() / 3600
            prev = (tp, w)
            yield tp, w, kwh

    def _get_factors(self, path_template, sn):
        ''' Equipment endpoints return groups of factors; flatten them to
            {code: (value, unit)}, numbers converted to float. '''
        groups = self.call(path_template.format(sn=sn), method="GET",
                           params={"deviceType": "INVERTER", "pwId": self.system_id})
        values = {}
        for group in groups if isinstance(groups, list) else []:
            for factor in group.get("factors") or []:
                code = factor.get("code")
                value = factor.get("data")
                if code is None or value is None:
                    continue
                if factor.get("dataType", "NUMERIC") == "NUMERIC":
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        if factor.get("dataType") == "NUMERIC":
                            continue
                values[code] = (value, factor.get("unit"))
        return values

    def getTelemetry(self, sn):
        ''' Live values, e.g. 'pAc': (0.927, 'kW'), 'Vac': (235.0, 'V'). '''
        return self._get_factors(TELEMETRY_PATH, sn)

    def getTelecounting(self, sn):
        ''' Energy counters, e.g. 'proPvStatsToday': (8.5, 'kWh'). '''
        return self._get_factors(TELECOUNTING_PATH, sn)

    @staticmethod
    def _to_watts(value, unit):
        return value * 1000 if unit and unit.lower().startswith("k") else value

    @staticmethod
    def _to_kwh(value, unit):
        u = (unit or "kWh").lower()
        if u.startswith("mwh"):
            return value * 1000
        if u == "wh":
            return value / 1000
        return value

    def _detect_curve_scale(self, curve_w, live_w):
        ''' The curve's unit isn't labelled; compare it with the live power once. '''
        global POWER_SCALE_TO_W
        if POWER_SCALE_TO_W != 1 or curve_w <= 0 or live_w < 50:
            return
        if abs(curve_w * 1000 - live_w) < abs(curve_w - live_w):
            POWER_SCALE_TO_W = 1000
            logging.info("Power curve appears to be in kW, scaling by 1000")

    def getCurrentReadings(self):
        ''' Most recent readings: live telemetry when available, energy from the curve. '''
        result = {
            'status': 'Offline',
            'pgrid_w': 0,
            'eday_kwh': 0,
            'etotal_kwh': 0,
            'grid_voltage': 0,
            'pv_voltage': 0,
            'temperature': None,
            'latitude': None,
            'longitude': None,
        }

        telemetry, counting = {}, {}
        sn = self.inverter_sn
        if sn:
            try:
                counting = self.getTelecounting(sn)
            except Exception as exp:
                logging.warning("Telecounting unavailable, computing energy from power curve: %s", exp)
            try:
                telemetry = self.getTelemetry(sn)
            except Exception as exp:
                logging.warning("Telemetry unavailable: %s", exp)
            if 'pAc' not in telemetry and 'pAc' in counting:
                telemetry['pAc'] = counting['pAc']

        samples = self.getPowerCurve(date_cls.today())

        if 'pAc' in telemetry:
            live_w = self._to_watts(*telemetry['pAc'])
            if samples and samples[-1][1] > 0 and POWER_SCALE_TO_W == 1:
                self._detect_curve_scale(samples[-1][1], live_w)
                if POWER_SCALE_TO_W != 1:
                    samples = [(tp, w * POWER_SCALE_TO_W) for tp, w in samples]
            result['pgrid_w'] = round(live_w)
            result['status'] = 'Normal' if live_w > 0 else 'Waiting'
            if 'Vac' in telemetry:
                result['grid_voltage'] = round(telemetry['Vac'][0], 1)
            pv = [v for code, (v, unit) in telemetry.items()
                  if code.endswith(':Vpv') and v and v < 6553]
            result['pv_voltage'] = round(sum(pv), 1)
            if 'Temperature' in telemetry:
                result['temperature'] = telemetry['Temperature'][0]

        if samples:
            points = list(self._cumulative_kwh(samples))
            last_tp, last_w, eday = points[-1]
            result['eday_kwh'] = round(eday, 3)
            fresh = datetime.now() - last_tp <= STALE_AFTER
            if 'pAc' not in telemetry and fresh:
                result['pgrid_w'] = round(last_w)
                result['status'] = 'Normal' if last_w > 0 else 'Waiting'
        else:
            fresh = False

        # Counters reported by the inverter beat our own integration
        if 'proPvStatsToday' in counting:
            result['eday_kwh'] = self._to_kwh(*counting['proPvStatsToday'])
        if 'proPvStatsTotal' in counting:
            result['etotal_kwh'] = self._to_kwh(*counting['proPvStatsTotal'])

        if not fresh:
            # Inverter not reporting (e.g. at night): telemetry would be stale
            result['status'] = 'Offline'
            result['pgrid_w'] = 0

        message = ("{status}, {pgrid_w} W now, {eday_kwh} kWh today, {etotal_kwh} kWh all time, "
                   "{grid_voltage} V grid, {pv_voltage} V PV").format(**result)
        if result['status'] in ('Normal', 'Offline', 'Waiting'):
            logging.info(message)
        else:
            logging.warning(message)

        return result

    def getLocation(self):
        return {'latitude': None, 'longitude': None}

    def getDayReadings(self, date):
        ''' Readings for a past day, for gw2pvo --date. '''
        day = date.date() if isinstance(date, datetime) else date
        result = self.getLocation()
        result['entries'] = [
            {'dt': tp, 'pgrid_w': round(w), 'eday_kwh': round(kwh, 3)}
            for tp, w, kwh in self._cumulative_kwh(self.getPowerCurve(day))
            if w > 0
        ]
        return result

    def statusText(self, status):
        labels = { -1 : 'Offline', 0 : 'Waiting', 1 : 'Normal', 2: 'Fault' }
        return labels[status] if status in labels else 'Unknown'

    def parseValue(self, value, unit):
        try:
            return float(value.rstrip(unit))
        except ValueError as exp:
            logging.warning(exp)
            return 0


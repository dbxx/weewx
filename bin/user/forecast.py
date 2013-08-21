# $Id$
# Copyright 2013 Matthew Wall
"""weewx module that provides forecasts

Configuration

   Some parameters can be defined in the Forecast section, then overridden
   for specific forecasting methods as needed.

[Forecast]
    # how often to calculate the forecast, in seconds
    interval = 300
    # how long to keep old forecasts, in seconds.  use None to keep forever.
    max_age = 604800
    # the database in which to record forecast information
    database = forecast_sqlite

    [[Zambretti]]
        # hemisphere can be NORTH or SOUTH
        hemisphere = NORTH

    [[NWS]]
        # first figure out your forecast office identifier (foid), then request
        # a point forecast using a url of this form in a web browser:
        #   http://forecast.weather.gov/product.php?site=NWS&product=PFM&format=txt&issuedby=YOUR_THREE_LETTER_FOID
        # scan the output for a service location identifier corresponding
        # to your location.

        # how often to download the forecast, in seconds
        interval = 10800
        # national weather service location identifier
        id = MAZ014
        # national weather service forecast office identifier
        foid = BOX
        # url for point forecast matrix
        url = http://forecast.weather.gov/product.php?site=NWS&product=PFM&format=txt

    [[WU]]
        # how often to download the forecast, in seconds
        interval = 10800
        # an api key is required to access the weather underground.
        # obtain an api key here:
        #   http://www.wunderground.com/weather/api/
        api_key = KEY
        # the location for the forecast can be one of the following:
        #   CA/San_Francisco     - US state/city
        #   60290                - US zip code
        #   Australia/Sydney     - Country/City
        #   37.8,-122.4          - latitude,longitude
        #   KJFK                 - airport code
        #   pws:KCASANFR70       - PWS id
        #   autoip               - AutoIP address location
        #   autoip.json?geo_ip=38.102.136.138 - specific IP address location
        # if no location is specified, station latitude and longitude are used
        location = 02139

    [[NOAATides]]
        # url for a specific 2-day tide forecast
        url = http://tidesandcurrents.noaa.gov/noaatidepredictions/download
        # how often to download the forecast, in seconds
        interval = 86400
        # how long to keep old tides, in seconds.  use None to keep forever.
        max_age = 17280

[Databases]
    ...
    [[forecast_sqlite]]
        root = %(WEEWX_ROOT)s
        database = archive/forecast.sdb
        driver = weedb.sqlite

    [[forecast_mysql]]
        host = localhost
        user = weewx
        password = weewx
        database = forecast
        driver = weedb.mysql

[Engines]
    [[WxEngine]]
        service_list = ... , user.forecast.ZambrettiForecast, user.forecast.NWSForecast, user.forecast.WUForecast
"""


"""
sample summary sites

http://www.tides4fishing.com/

http://www.surf-forecast.com/

http://ocean.peterbrueggeman.com/tidepredict.html
"""

# TODO: single table with unused fields, one table per method, or one db per ?
# TODO: add forecast data to skin variables

import httplib
import socket
import string
import subprocess
import syslog
import time
import urllib
import urllib2

import weewx
from weewx.wxengine import StdService
from weewx.filegenerator import FileGenerator
import weeutil.weeutil

try:
    import cjson as json
    setattr(json, 'dumps', json.encode)
    setattr(json, 'loads', json.decode)
except Exception, e:
    try:
        import simplejson as json
    except Exception, e:
        try:
            import json
        except Exception, e:
            json = None

def logdbg(msg):
    syslog.syslog(syslog.LOG_DEBUG, 'forecast: %s' % msg)

def loginf(msg):
    syslog.syslog(syslog.LOG_INFO, 'forecast: %s' % msg)

def logerr(msg):
    syslog.syslog(syslog.LOG_ERR, 'forecast: %s' % msg)

def get_int(config_dict, label, default_value):
    value = config_dict.get(label, default_value)
    if isinstance(value, str) and value.lower() == 'none':
        value = None
    if value is not None:
        try:
            value = int(value)
        except Exception, e:
            logerr("bad value '%s' for %s" % (value, label))
    return value

"""Forecast Schema

   This schema captures all forecasts and defines the following fields:

   method - forecast method, e.g., Zambretti, NWS
   dateTime - timestamp in seconds when forecast was made

   database     nws                    wu                    zambretti
   field        field                  field                 field
   -----------  ---------------------  --------------------  ---------
   method
   dateTime
   usUnits

   zcode                                                     CODE

   foid
   id
   source
   desc
   ts
   hour         3HRLY | 6HRLY          date.hour
   tempMin      MIN/MAX | MAX/MIN      low.fahrenheit
   tempMax      MIN/MAX | MAX/MIN      high.fahrenheit
   temp         TEMP
   dewpoint     DEWPT
   humidity     RH                     avehumidity
   windDir      WIND DIR | PWIND DIR   avewind.dir
   windSpeed    WIND SPD               avewind.mph
   windGust     WIND GUST              maxwind.mph
   windChar     WIND CHAR
   clouds       CLOUDS | AVG CLOUNDS
   pop          POP 12HR               pop
   qpf          QPF 12HR               qpf_allday.in
   qsf          SNOW 12HR              snow_allday.in
   rain         RAIN
   rainshwrs    RAIN SHWRS
   tstms        TSTMS
   drizzle      DRIZZLE
   snow         SNOW
   snowshwrs    SNOW SHWRS
   flurries     FLURRIES
   sleet        SLEET
   frzngrain    FRZNG RAIN
   frzngdrzl    FRZNG DRZL
   obvis        OBVIS
   windChill    WIND CHILL
   heatIndex    HEAT INDEX

   hilo         indicates whether this is a high or low tide
   offset       how high or low the tide is relative to mean low
"""
defaultForecastSchema = [('method',     'VARCHAR(10) NOT NULL'),
                         ('dateTime',   'INTEGER NOT NULL'),
                         ('usUnits',    'INTEGER NOT NULL'),

                         # Zambretti fields
                         ('zcode',      'CHAR(1)'),

                         # NWS fields
                         ('foid',       'CHAR(3)'),     # e.g., BOX
                         ('id',         'CHAR(6)'),     # e.g., MAZ014
                         ('ts',         'INTEGER'),     # seconds
                         ('hour',       'INTEGER'),     # 00 to 23
                         ('tempMin',    'REAL'),        # degree F
                         ('tempMax',    'REAL'),        # degree F
                         ('temp',       'REAL'),        # degree F
                         ('dewpoint',   'REAL'),        # degree F
                         ('humidity',   'REAL'),        # percent
                         ('windDir',    'VARCHAR(3)'),  # N,NE,E,SE,S,SW,W,NW
                         ('windSpeed',  'REAL'),        # mph
                         ('windGust',   'REAL'),        # mph
                         ('windChar',   'VARCHAR(2)'),  # GN,LT
                         ('clouds',     'VARCHAR(2)'),  # CL,SC,BK,OV, ...
                         ('pop',        'REAL'),        # percent
                         ('qpf',        'REAL'),        # inch
                         ('qsf',        'VARCHAR(5)'),  # inch
                         ('rain',       'VARCHAR(2)'),  # S,C,L,O,D
                         ('rainshwrs',  'VARCHAR(2)'),
                         ('tstms',      'VARCHAR(2)'),
                         ('drizzle',    'VARCHAR(2)'),
                         ('snow',       'VARCHAR(2)'),
                         ('snowshwrs',  'VARCHAR(2)'),
                         ('flurries',   'VARCHAR(2)'),
                         ('sleet',      'VARCHAR(2)'),
                         ('frzngrain',  'VARCHAR(2)'),
                         ('frzngdrzl',  'VARCHAR(2)'),
                         ('obvis',      'VARCHAR(3)'),  # F,PF,F+,PF+,H,BS,K,BD
                         ('windChill',  'REAL'),        # degree F
                         ('heatIndex',  'REAL'),        # degree F

                         # NOAA tide fields
                         ('hilo',     'CHAR(1)'),       # H or L
                         ('offset',   'REAL'),          # relative to mean low
                         ]

"""Tides Schema
   This schema captures tidal information.
"""
defaultTideSchema = [('location', 'VARCHAR(16) NOT NULL'),
                     ('dateTime', 'INTEGER NOT NULL'),
                     ('usUnits',  'INTEGER NOT NULL'),
                     ('hilo',     'CHAR(1)'),       # H or L
                     ('offset',   'REAL'),          # relative to mean low
                     ]

class Forecast(StdService):
    """Provide forecast."""

    def __init__(self, engine, config_dict, fid, defaultSchema=defaultForecastSchema, table='archive'):
        super(Forecast, self).__init__(engine, config_dict)
        d = config_dict['Forecast'] if 'Forecast' in config_dict.keys() else {}
        self.interval = get_int(d, 'interval', 300)
        self.max_age = get_int(d, 'max_age', 604800)

        dd = config_dict['Forecast'][fid] \
            if fid in config_dict['Forecast'].keys() else {}
        self.max_age = get_int(dd, 'max_age', self.max_age)
        schema_str = dd['schema'] \
            if 'schema' in dd.keys() else d.get('schema', None)
        schema = weeutil.weeutil._get_object(schema_str) \
            if schema_str is not None else defaultSchema
        dbid = dd['database'] \
            if 'database' in dd.keys() else d['database']

        self.method_id = fid
        self.last_ts = 0
        self.setup_database(config_dict, dbid, schema, table)
        self.bind(weewx.NEW_ARCHIVE_RECORD, self.update_forecast)

    def update_forecast(self, event):
        now = time.time()
        if self.last_ts is not None \
                and self.interval is not None \
                and now - self.interval < self.last_ts:
            logdbg('not yet time to do the %s forecast' % self.method_id)
            return
        fcast = self.get_forecast(event)
        if fcast is None:
            return
        self.save_forecast(fcast)
        self.last_ts = now
        if self.max_age is not None:
            self.prune_forecasts(now - self.max_age)

    def get_forecast(self, event):
        """get the forecast, return a forecast record"""
        return None

    def save_forecast(self, record):
        """add a forecast record to the forecast database

        record - dictionary with keys corresponding to database fields
        """
        self.archive.addRecord(record)

    def prune_forecasts(self, ts):
        """remove old forecasts from the database
        
        method_id - string that indicates the forecast method

        ts - timestamp, in seconds.  records older than this will be deleted.
        """
        sql = "delete from %s where method = '%s' and dateTime < %d" % (self.table, self.method_id, ts)
        cursor = self.archive.connection.cursor()
        try:
            cursor.execute(sql)
            loginf('%s: deleted forecasts prior to %d' % (self.method_id, ts))
        except Exception, e:
            logerr('%s: unable to delete old records: %s' %
                   (self.method_id, e))

    def get_saved_forecasts(self, since_ts=None):
        """return saved forecasts since the indicated timestamp

        since_ts - timestamp, in seconds.  a value of None will return all.
        """
        sql = "select * from %s where method = '%s'" % (self.table, self.method_id)
        if since_ts is not None:
            sql += " and dateTime > %d" % since_ts
        records = []
        for r in self.archive.genSql(sql):
            records.append(r)
        return records

    def setup_database(self, config_dict, dbid, schema, table):
        self.table = table
        self.archive = weewx.archive.Archive.open_with_create(config_dict['Databases'][dbid], schema, table)
        loginf('%s: using table %s in database %s' % (self.method_id, table, dbid))


# -----------------------------------------------------------------------------
# Zambretti Forecaster
#
# The zambretti forecast is based upon recent weather conditions.  Supposedly
# it is about 90% to 94% accurate.  It is simply a table of values based upon
# the current barometric pressure, pressure trend, winter/summer, and wind
# direction.
#
# http://www.meteormetrics.com/zambretti.htm
# -----------------------------------------------------------------------------

Z_KEY = 'Zambretti'

class ZambrettiForecast(Forecast):
    """calculate zambretti code"""

    def __init__(self, engine, config_dict):
        super(ZambrettiForecast, self).__init__(engine, config_dict, Z_KEY)
        d = config_dict['Forecast'][Z_KEY] \
            if Z_KEY in config_dict['Forecast'].keys() else {}
        self.hemisphere = d.get('hemisphere', 'NORTH')
        loginf('%s: interval=%s max_age=%s hemisphere=%s' %
               (Z_KEY, self.interval, self.max_age, self.hemisphere))

    def get_forecast(self, event):
        record = event.record
        ts = record['dateTime']
        if ts is None:
            logerr('%s: skipping forecast: null timestamp in archive record' %
                   Z_KEY)
            return None
        tt = time.gmtime(ts)
        pressure = record['barometer']
        month = tt.tm_mon - 1 # month is [0-11]
        wind = int(record['windDir'] / 22.5) # wind dir is [0-15]
        north = self.hemisphere.lower() != 'south'
        logdbg('%s: pressure=%s month=%s wind=%s north=%s' %
               (Z_KEY, pressure, month, wind, north))
        code = ZambrettiCode(pressure, month, wind, north)
        logdbg('%s: code is %s' % (Z_KEY, code))
        if code is None:
            return None

        record = {}
        record['usUnits'] = weewx.US
        record['method'] = Z_KEY
        record['dateTime'] = ts
        record['zcode'] = code
        loginf('%s: generated 1 forecast record' % Z_KEY)
        return record

zambretti_dict = {
    'A' : "Settled fine",
    'B' : "Fine weather",
    'C' : "Becoming fine",
    'D' : "Fine, becoming less settled",
    'E' : "Fine, possible showers",
    'F' : "Fairly fine, improving",
    'G' : "Fairly fine, possible showers early",
    'H' : "Fairly fine, showery later",
    'I' : "Showery early, improving",
    'J' : "Changeable, mending",
    'K' : "Fairly fine, showers likely",
    'L' : "Rather unsettled clearing later",
    'M' : "Unsettled, probably improving",
    'N' : "Showery, bright intervals",
    'O' : "Showery, becoming less settled",
    'P' : "Changeable, some rain",
    'Q' : "Unsettled, short fine intervals",
    'R' : "Unsettled, rain later",
    'S' : "Unsettled, some rain",
    'T' : "Mostly very unsettled",
    'U' : "Occasional rain, worsening",
    'V' : "Rain at times, very unsettled",
    'W' : "Rain at frequent intervals",
    'X' : "Rain, very unsettled",
    'Y' : "Stormy, may improve",
    'Z' : "Stormy, much rain"
    }

def ZambrettiText(code):
    return zambretti_dict[code]

def ZambrettiCode(pressure, month, wind, trend,
                  north=True, baro_top=1050.0, baro_bottom=950.0):
    """Simple implementation of Zambretti forecaster algorithm based on
    implementation in pywws, inspired by beteljuice.com Java algorithm,
    as converted to Python by honeysucklecottage.me.uk, and further
    information from http://www.meteormetrics.com/zambretti.htm

    pressure - barometric pressure in millibars

    month - month of the year as number in [0,11]

    wind - wind direction as number in [0,16]

    trend - pressure change in millibars
    """

    if pressure is None:
        return None
    if month < 0 or month > 11:
        return None
    if wind < 0 or wind > 15:
        return None

    # normalise pressure
    pressure = 950.0 + ((1050.0 - 950.0) *
                        (pressure - baro_bottom) / (baro_top - baro_bottom))
    # adjust pressure for wind direction
    if wind is not None:
        if not north:
            # southern hemisphere, so add 180 degrees
            wind = (wind + 8) % 16
        pressure += (  5.2,  4.2,  3.2,  1.05, -1.1, -3.15, -5.2, -8.35,
                     -11.5, -9.4, -7.3, -5.25, -3.2, -1.15,  0.9,  3.05)[wind]
    # compute base forecast from pressure and trend (hPa / hour)
    if trend >= 0.1:
        # rising pressure
        if north == (month >= 4 and month <= 9):
            pressure += 3.2
        F = 0.1740 * (1031.40 - pressure)
        LUT = ('A','B','B','C','F','G','I','J','L','M','M','Q','T','Y')
    elif trend <= -0.1:
        # falling pressure
        if north == (month >= 4 and month <= 9):
            pressure -= 3.2
        F = 0.1553 * (1029.95 - pressure)
        LUT = ('B','D','H','O','R','U','V','X','X','Z')
    else:
        # steady
        F = 0.2314 * (1030.81 - pressure)
        LUT = ('A','B','B','B','E','K','N','N','P','P','S','W','W','X','X','X','Z')
    # clip to range of lookup table
    F = min(max(int(F + 0.5), 0), len(LUT) - 1)
    # convert to letter code
    return LUT[F]


# -----------------------------------------------------------------------------
# US National Weather Service Point Forecast Matrix
#
# For an explanation of point forecasts, see:
#   http://www.srh.weather.gov/jetstream/webweather/pinpoint_max.htm
#
# For details about how to decode the NWS point forecast matrix, see:
#   http://www.srh.noaa.gov/bmx/?n=pfm
#
# For actual forecasts, see:
#   http://www.weather.gov/
#
# For example:
#   http://forecast.weather.gov/product.php?site=NWS&product=PFM&format=txt&issuedby=BOX
#
# codes for clouds:
#   CL - clear
#   FW - mostly clear
#   SC - partly cloudy
#   BK - mostly cloudy
#   OV - cloudy
#   B1 -
#   B2 - 
#
# codes for rain, drizzle, flurries, etc:
#   S - slight chance (< 20%)
#   C - chance (30%-50%)
#   L - likely (60%-70%)
#   O - occasional (80%-100%)
#   D - definite (80%-100%)
#
# codes for obvis (obstruction to visibility):
#   F   - fog
#   PF  - patchy fog
#   F+  - dense fog
#   PF+ - patchy dense fog
#   H   - haze
#   BS  - blowing snow
#   K   - smoke
#   BD  - blowing dust
#
# codes for wind char:
#   LT - 
#   GN - 
#
# -----------------------------------------------------------------------------

# The default URL contains the bare minimum to request a point forecast, less
# the forecast office identifier.
DEFAULT_NWS_PFM_URL = 'http://forecast.weather.gov/product.php?site=NWS&product=PFM&format=txt'

NWS_KEY = 'NWS'

class NWSForecast(Forecast):
    """Download forecast from US National Weather Service."""

    def __init__(self, engine, config_dict):
        super(NWSForecast, self).__init__(engine, config_dict, NWS_KEY)
        d = config_dict['Forecast'][NWS_KEY] \
            if NWS_KEY in config_dict['Forecast'].keys() else {}
        self.interval = get_int(d, 'interval', 10800)
        self.url = d.get('url', DEFAULT_NWS_PFM_URL)
        self.max_tries = d.get('max_tries', 3)
        self.id = d.get('id', None)
        self.foid = d.get('foid', None)

        errmsg = []
        if self.id is None:
            errmsg.append('NWS location ID (id) is not specified')
        if self.foid is None:
            errmsg.append('NWS forecast office ID (foid) is not specified')
        if len(errmsg) > 0:
            raise Exception, '\n'.join(errmsg)

        loginf('%s: interval=%s max_age=%s id=%s foid=%s' %
               (NWS_KEY, self.interval, self.max_age, self.id, self.foid))

    def get_forecast(self, event):
        text = DownloadNWSForecast(self.foid, self.url, self.max_tries)
        if text is None:
            logerr('%s: no PFM data for %s from %s' %
                   (NWS_KEY, self.foid, self.url))
            return None
        matrix = ParseNWSForecast(text, self.id)
        if matrix is None:
            logerr('%s: no PFM found for %s in forecast from %s' %
                   (NWS_KEY, self.id, self.foid))
            return None
        logdbg('%s: forecast matrix: %s' % (NWS_KEY, matrix))

        records = []
        for i,ts in enumerate(matrix['ts']):
            record = {}
            record['usUnits'] = weewx.US
            record['method'] = NWS_KEY
            record['dateTime'] = matrix['dateTime']
            record['ts'] = ts
            record['id'] = self.id
            record['foid'] = self.foid
            for label in matrix.keys():
                if isinstance(matrix[label], list):
                    record[label] = matrix[label][i]
            records.append(record)
        loginf('%s: got %d forecast records' % (NWS_KEY, len(records)))

        return records

# mapping of NWS names to database fields
nws_label_dict = {
    'HOUR'       : 'hour',
    'MIN/MAX'    : 'tempMinMax',
    'MAX/MIN'    : 'tempMaxMin',
    'TEMP'       : 'temp',
    'DEWPT'      : 'dewpoint',
    'RH'         : 'humidity',
    'WIND DIR'   : 'windDir',
    'PWIND DIR'  : 'windDir',
    'WIND SPD'   : 'windSpeed',
    'WIND GUST'  : 'windGust',
    'WIND CHAR'  : 'windChar',
    'CLOUDS'     : 'clouds',
    'AVG CLOUDS' : 'clouds',
    'POP 12HR'   : 'pop',
    'QPF 12HR'   : 'qpf',
    'SNOW 12HR'  : 'qsf',
    'RAIN'       : 'rain',
    'RAIN SHWRS' : 'rainshwrs',
    'TSTMS'      : 'tstms',
    'DRIZZLE'    : 'drizzle',
    'SNOW'       : 'snow',
    'SNOW SHWRS' : 'snowshwrs',
    'FLURRIES'   : 'flurries',
    'SLEET'      : 'sleet',
    'FRZNG RAIN' : 'frzngrain',
    'FRZNG DRZL' : 'frzngdrzl',
    'OBVIS'      : 'obvis',
    'WIND CHILL' : 'windChill',
    'HEAT INDEX' : 'heatIndex',
    }

def DownloadNWSForecast(foid, url=DEFAULT_NWS_PFM_URL, max_tries=3):
    """Download a point forecast matrix from the US National Weather Service"""

    u = '%s&issuedby=%s' % (url, foid) if url == DEFAULT_NWS_PFM_URL else url
    logdbg("%s: downloading forecast from '%s'" % (NWS_KEY, u))
    for count in range(max_tries):
        try:
            response = urllib2.urlopen(u)
            text = response.read()
            return text
        except (urllib2.URLError, socket.error,
                httplib.BadStatusLine, httplib.IncompleteRead), e:
            logerr('%s: failed attempt %d to download NWS forecast: %s' %
                   (NWS_KEY, count+1, e))
    else:
        logerr('%s: failed to download forecast' % NWS_KEY)
    return None

def ParseNWSForecast(text, id):
    """Parse a United States National Weather Service point forcast matrix.
    Save it into a dictionary with per-hour elements for wind, temperature,
    etc. extracted from the point forecast.
    """

    alllines = text.splitlines()
    lines = None
    for line in iter(alllines):
        if line.startswith(id):
            lines = []
            lines.append(line)
        elif lines is not None:
            if line.startswith('$$'):
                break
            else:
                lines.append(line)
    if lines is None:
        return None

    rows3 = {}
    rows6 = {}
    ts = date2ts(lines[3])
    day_ts = weeutil.weeutil.startOfDay(ts)
    for line in lines:
        label = line[0:14].strip()
        if label.startswith('UTC'):
            continue
        if label.endswith('3HRLY'):
            label = 'HOUR'
            mode = 3
        elif label.endswith('6HRLY'):
            label = 'HOUR'
            mode = 6
        if label in nws_label_dict.keys():
            if mode == 3:
                rows3[nws_label_dict[label]] = line[14:]
            elif mode == 6:
                rows6[nws_label_dict[label]] = line[14:]

    matrix = {}
    matrix['id'] = id
    matrix['desc'] = lines[1]
    matrix['location'] = lines[2]
    matrix['dateTime'] = ts
    matrix['ts'] = []
    matrix['hour'] = []

    idx = 0
    day = day_ts
    lasth = None

    # get the 3-hour indexing
    indices3 = {}
    for i in range(0, len(rows3['hour']), 3):
        h = int(rows3['hour'][i:i+2])
        if lasth is not None and h < lasth:
            day += 24 * 3600
        lasth = h
        matrix['ts'].append(day + h*3600)
        matrix['hour'].append(h)
        indices3[i+1] = idx
        idx += 1
    nidx3 = idx

    # get the 6-hour indexing
    indices6 = {}
    s = ''
    for i in range(0, len(rows6['hour'])):
        if rows6['hour'][i].isspace():
            if len(s) > 0:
                h = int(s)
                if lasth is not None and h < lasth:
                    day += 24 * 3600
                lasth = h
                matrix['ts'].append(day + h*3600)
                matrix['hour'].append(h)
                indices6[i-1] = idx
                idx += 1
            s = ''
        else:
            s += rows6['hour'][i]
    if len(s) > 0:
        h = int(s)
        matrix['ts'].append(day + h*3600)
        matrix['hour'].append(h)
        indices6[len(rows6['hour'])-1] = idx
        idx += 1

    # get the 3 and 6 hour data
    filldata(matrix, idx, rows3, indices3)
    filldata(matrix, idx, rows6, indices6)
    return matrix

def filldata(matrix, nidx, rows, indices):
    """fill matrix with data from rows"""
    for label in rows.keys():
        if label not in matrix.keys():
            matrix[label] = [None]*nidx
        s = ''
        for i in range(0, len(rows[label])):
            if rows[label][i].isspace():
                if len(s) > 0:
                    matrix[label][indices[i-1]] = s
                s = ''
            else:
                s += rows[label][i]
        if len(s) > 0:
            matrix[label][indices[len(rows[label])-1]] = s

    # deal with min/max temperatures
    if 'tempMin' not in matrix.keys():
        matrix['tempMin'] = [None]*nidx
    if 'tempMax' not in matrix.keys():
        matrix['tempMax'] = [None]*nidx
    if 'tempMinMax' in matrix.keys():
        state = 0
        for i in range(nidx):
            if matrix['tempMinMax'][i] is not None:
                if state == 0:
                    matrix['tempMin'][i] = matrix['tempMinMax'][i]
                    state = 1
                else:
                    matrix['tempMax'][i] = matrix['tempMinMax'][i]
                    state = 0
        del matrix['tempMinMax']
    if 'tempMaxMin' in matrix.keys():
        state = 1
        for i in range(nidx):
            if matrix['tempMaxMin'][i] is not None:
                if state == 0:
                    matrix['tempMin'][i] = matrix['tempMaxMin'][i]
                    state = 1
                else:
                    matrix['tempMax'][i] = matrix['tempMaxMin'][i]
                    state = 0
        del matrix['tempMaxMin']

def date2ts(tstr):
    """Convert NWS date string to timestamp in seconds.
    sample format: 418 PM EDT SAT MAY 11 2013
    """

    parts = tstr.split(' ')
    s = '%s %s %s %s' % (parts[0], parts[4], parts[5], parts[6])
    ts = time.mktime(time.strptime(s, "%H%M %b %d %Y"))
    if parts[1] == 'PM':
        ts += 12 * 3600
    return int(ts)


# -----------------------------------------------------------------------------
# Weather Underground Forecasts
#
# Forecasts from the weather underground (www.wunderground.com).  WU provides
# an api that returns json/xml data.  This implementation uses the json format.
#
# For the weather underground api, see:
#   http://www.wunderground.com/weather/api/d/docs?MR=1
# -----------------------------------------------------------------------------

WU_KEY = 'WU'

DEFAULT_WU_URL = 'http://api.wunderground.com/api'

class WUForecast(Forecast):
    """Download forecast from Weather Underground."""

    def __init__(self, engine, config_dict):
        super(WUForecast, self).__init__(engine, config_dict, WU_KEY)
        d = config_dict['Forecast'][WU_KEY] \
            if WU_KEY in config_dict['Forecast'].keys() else {}
        self.interval = get_int(d, 'interval', 10800)
        self.url = d.get('url', DEFAULT_WU_URL)
        self.max_tries = d.get('max_tries', 3)
        self.api_key = d.get('api_key', None)
        self.location = d.get('location', None)

        if self.location is None:
            lat = config_dict['Station'].get('latitude', None)
            lon = config_dict['Station'].get('longitude', None)
            if lat is not None and lon is not None:
                self.location = '%s,%s' % (lat,lon)

        errmsg = []
        if json is None:
            errmsg.appen('json is not installed')
        if self.api_key is None:
            errmsg.append('WU API key (api_key) is not specified')
        if self.location is None:
            errmsg.append('WU location is not specified')
        if len(errmsg) > 0:
            raise Exception, '\n'.join(errmsg)

        loginf('%s: interval=%s max_age=%s api_key=%s location=%s' %
               (WU_KEY, self.interval, self.max_age, self.api_key, self.location))

    def get_forecast(self, event):
        text = DownloadWUForecast(self.api_key, self.location, self.url, self.max_tries)
        if text is None:
            logerr('%s: no forecast data for %s from %s' %
                   (WU_KEY, self.location, self.url))
            return None
        matrix = ProcessWUForecast(text)
        if matrix is None:
            return None
        logdbg('%s: forecast matrix: %s' % (WU_KEY, matrix))

        records = []
        for i,ts in enumerate(matrix['ts']):
            record = {}
            record['usUnits'] = weewx.US
            record['method'] = WU_KEY
            record['dateTime'] = matrix['dateTime']
            record['ts'] = ts
            for label in matrix.keys():
                if isinstance(matrix[label], list):
                    record[label] = matrix[label][i]
            records.append(record)
        loginf('%s: got %d forecast records' % (WU_KEY, len(records)))

        return records

def DownloadWUForecast(api_key, location, url=DEFAULT_WU_URL, max_tries=3):
    """Download a forecast from the Weather Underground"""

    u = '%s/%s/forecast10day/q/%s.json' % (url, api_key, location) \
        if url == DEFAULT_WU_URL else url
    logdbg("%s: downloading forecast from '%s'" % (WU_KEY, u))
    for count in range(max_tries):
        try:
            response = urllib2.urlopen(u)
            text = response.read()
            return text
        except (urllib2.URLError, socket.error,
                httplib.BadStatusLine, httplib.IncompleteRead), e:
            logerr('%s: failed attempt %d to download WU forecast: %s' %
                   (WU_KEY, count+1, e))
    else:
        logerr('%s: failed to download forecast' % WU_KEY)
    return None

def ProcessWUForecast(text):
    obj = json.loads(text)
    if not 'response' in obj.keys():
        logerr('%s: unknown format in response' % WU_KEY)
        return None
    response = obj['response']
    if 'error' in response.keys():
        logerr('%s: error in response: %s: %s' %
               (WU_KEY,
                response['error']['type'], response['error']['description']))
        return None

    fc = obj['forecast']['simpleforecast']['forecastday']
    tstr = obj['forecast']['txt_forecast']['date']
    ts = int(time.time())

    matrix = {}
    matrix['dateTime'] = ts
    matrix['ts'] = []
    matrix['hour'] = []
    matrix['tempMin'] = []
    matrix['tempMax'] = []
    matrix['humidity'] = []
    matrix['pop'] = []
    matrix['qpf'] = []
    matrix['qsf'] = []
    matrix['windSpeed'] = []
    matrix['windDir'] = []
    matrix['windGust'] = []
    for i,period in enumerate(fc):
        try:
            matrix['ts'].append(int(period['date']['epoch']))
            matrix['hour'].append(period['date']['hour'])
            try:
                matrix['tempMin'].append(float(period['low']['fahrenheit']))
            except Exception, e:
                logerr('%s: bogus tempMin in forecast: %s' % (WU_KEY, e))
            try:
                matrix['tempMax'].append(float(period['high']['fahrenheit']))
            except Exception, e:
                logerr('%s: bogus tempMax in forecast: %s' % (WU_KEY, e))
            matrix['humidity'].append(period['avehumidity'])
            matrix['pop'].append(period['pop'])
            matrix['qpf'].append(period['qpf_allday']['in'])
            matrix['qsf'].append(period['snow_allday']['in'])
            matrix['windSpeed'].append(period['avewind']['mph'])
            matrix['windDir'].append(dirstr(period['avewind']['dir']))
            matrix['windGust'].append(period['maxwind']['mph'])
        except Exception, e:
            logerr('%s: bad timestamp in forecast: %s' % (WU_KEY, e))

    return matrix

def dirstr(s):
    directions = {'North':'N',
                  'South':'S',
                  'East':'E',
                  'West':'W',
                  }
    s = str(s)
    if s in directions.keys():
        s = directions[s]
    return s


class TideForecast(Forecast):
    """generic tide forecaster, downloads tides from internet"""

    def __init__(self, engine, config_dict, key):
        super(TideForecast, self).__init__(engine, config_dict, key)
        d = config_dict['Forecast'][key] \
            if key in config_dict['Forecast'].keys() else {}
        self.max_tries = d.get('max_tries', 3)

    def get_forecast(self, event):
        text = self.download_forecast()
        if text is None:
            logerr('%s: no tide data found' % self.method_id)
            return None
        matrix = self.parse_forecast()
        if matrix is None:
            logerr('%s: no tides found in tide data' % self.method_id)
            return None
        logdbg('%s: tide matrix: %s' % (self.method_id, matrix))

        records = []
        for i,ts in enumerate(matrix['ts']):
            record = {}
            record['usUnits'] = weewx.US
            record['method'] = self.method_id
            record['dateTime'] = matrix['dateTime']
            record['ts'] = ts
            record['location'] = matrix['location']
            record['hilo'] = matrix['hilo']
            record['offset'] = matrix['offset']
        return records

    def download_forecast(self):
        return None

    def parse_forecast(self, text):
        return None

"""saltwatertides.com tide predictor

i get http 500 internal server error when i try to download from saltwatertides

http://www.saltwatertides.com/cgi-local/neatlantic.cgi
site=Maine
station_number=8415809
month=08
year=2013
start_date=20
maximum_days=3
"""

SWT_KEY = 'SWTides'

class SaltwaterTidesForecast(TideForecast):
    """download tide forecast from saltwatertides.com"""

    def __init__(self, engine, config_dict):
        super(SaltwaterTidesForecast, self).__init__(engine, config_dict,
                                                     SWT_KEY)
        d = config_dict['Forecast'][self.method_id] \
            if self.method_id in config_dict['Forecast'].keys() else {}
        self.url = d['url']
        self.site = d['site']
        self.station_number = d['station_number']
        loginf('%s: interval=%s max_age=%s' %
               (self.method_id, self.interval, self.max_age))

    def download_forecast(self):
        return DownloadSaltwaterTides(self.url)

    def parse_forecast(self, text):
        return ParseSaltwaterTides(text)

def DownloadSaltwaterTides(url, site, station,
                           start_date=None, month=None, year=None,
                           maximum_days='3', max_tries=3):
    """Download tides from saltwatertides.com tide predictor"""

    if start_date is None or month is None or year is None:
        now = time.time()
        ts = time.localtime(now)
        if start_date is None:
            start_date = ts.tm_mday
        if month is None:
            month = ts.tm_month
        if year is None:
            year = ts.tm_year
    logdbg("%s: downloading from '%s'" % (SWT_KEY, url))
    for count in range(max_tries):
        try:
            fields = {}
            fields['site'] = site
            fields['station_number'] = station
            fields['start_date'] = start_date
            fields['month'] = month
            fields['year'] = year
            fields['maximum_days'] = maximum_days
            loginf('fields: %s' % fields)
            request = urllib2.Request(url, urllib.urlencode(fields))
            response = urllib2.urlopen(request)
            text = response.read()
            alllines = text.splitlines()
            lines = None
            for line in iter(alllines):
                if line.startswith('Error Message Page'):
                    logerr('%s: download failed, server did not like request' %
                           SWT_KEY)
                    return None
            return text
        except (urllib2.URLError, socket.error,
                httplib.BadStatusLine, httplib.IncompleteRead), e:
            logerr('%s: failed attempt %d to download tides: %s' %
                   (SWT_KEY, count+1, e))
    else:
        logerr('%s: failed to download tides' % SWT_KEY)
    return None

def ParseSaltwaterTides(text):
    """Parse the output from saltwatertides.com tide predictor."""

    alllines = text.splitlines()
    lines = None
    for line in iter(alllines):
        pass

    return None



"""NOAA tide predictor

the web interface to noaa is horrendous - apparently NOAATidesFacade does some
magic to make the java app do the right thing, because if you just twiddle the
cgi arguments you get null pointer exceptions.

http://tidesandcurrents.noaa.gov/noaatidepredictions/NOAATidesFacade.jsp?Stationid=8415809

http://tidesandcurrents.noaa.gov/noaatidepredictions/viewDailyPredictions.jsp?bmon=08&bday=19&byear=2013&timelength=daily&timeZone=2&dataUnits=1&datum=MLLW&timeUnits=1&interval=highlow&format=Submit&Stationid=8415809

http://tidesandcurrents.noaa.gov/faq2.html

http://tidesandcurrents.noaa.gov/accuracy.html

http://tidesandcurrents.noaa.gov/tide_predictions.shtml
"""

NT_KEY = 'NOAATides'

class NOAATideForecast(TideForecast):
    """download tide forecast from NOAA"""

    def __init__(self, engine, config_dict):
        super(NOAATideForecast, self).__init__(engine, config_dict, NT_KEY)
        d = config_dict['Forecast'][NT_KEY] \
            if NT_KEY in config_dict['Forecast'].keys() else {}
        self.url = d['url']
        loginf('%s: interval=%s max_age=%s' %
               (NT_KEY, self.interval, self.max_age))

    def download_forecast(self):
        return DownloadNOAATides(self.url)

    def parse_forecast(self, text):
        return ParseNOAATides(text)

def DownloadNOAATides(url, max_tries=3):
    """Download tides from US NOAA tide predictor"""

    logdbg("%s: downloading from '%s'" % (NT_KEY, url))
    for count in range(max_tries):
        try:
            response = urllib2.urlopen(url)
            text = response.read()
            return text
        except (urllib2.URLError, socket.error,
                httplib.BadStatusLine, httplib.IncompleteRead), e:
            logerr('%s: failed attempt %d to download tides: %s' %
                   (NT_KEY, count+1, e))
    else:
        logerr('%s: failed to download tides' % NT_KEY)
    return None

def ParseNOAATides(text):
    """Parse the output from the US NOAA tide predictor."""

    alllines = text.splitlines()
    lines = None
    for line in iter(alllines):
        pass

    return None


"""xtide tide predictor
   The xtide application must be installed for this to work.
"""

XT_KEY = 'XTide'
XT_PROG = '/usr/bin/tide'
XT_ARGS = '-fc -df"%Y.%m.%d" -tf"%H:%M"'

class XTideForecast(Forecast):
    """generate tide forecast using xtide"""

    def __init__(self, engine, config_dict):
        super(XTideForecast, self).__init__(engine, config_dict, XT_KEY)
        d = config_dict['Forecast'][XT_KEY] \
            if XT_KEY in config_dict['Forecast'].keys() else {}
        self.interval = get_int(d, 'interval', 604800)
        self.tideprog = d.get('prog', XT_PROG)
        self.tideargs = d.get('args', XT_ARGS)
        self.location = d['location']
        loginf('%s: interval=%s max_age=%s' %
               (XT_KEY, self.interval, self.max_age))

    def get_forecast(self, event):
        lines = self.generate_tide()
        if lines is None:
            return None
        records = self.parse_forecast(lines)
        if records is None:
            return None
        logdbg('%s: tide matrix: %s' % (self.method_id, records))
        return records

    def generate_tide(self, st=None, et=None):
        if st is None or et is None:
            now = time.time()
            st = time.strftime('%Y-%m-%d %H:%M', time.localtime(now))
            et = time.strftime('%Y-%m-%d %H:%M', time.localtime(now+self.interval))
        cmd = "%s %s -l'%s' -b'%s' -e'%s'" % (self.tideprog, self.tideargs, self.location, st, et)
        try:
            loginf('%s: generating tides for %s days' % (XT_KEY, self.interval / (24*3600)))
            logdbg("%s: running command '%s'" % (XT_KEY, cmd))
            p = subprocess.Popen(cmd, shell=True,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
            rc = p.returncode
            if rc is not None:
                logerr('%s: generate tide failed: code=%s' % (XT_KEY, -rc))
                return None
            out = []
            for line in p.stdout:
                if string.find(line, 'Error') >= 0:
                    logerr('%s: generate tide failed: %s' % (XT_KEY, line))
                    return None
                if string.find(line, self.location) >= 0:
                    out.append(line)
            return out
        except OSError, e:
            logerr('%s: generate tide failed: %s' % (XT_KEY, e))
        return None

    def parse_forecast(self, lines, now=None):
        hilo = {}
        hilo['High Tide'] = 'H'
        hilo['Low Tide'] = 'L'
        if now is None:
            now = int(time.time())
        records = []
        for line in lines:
            line = string.rstrip(line)
            fields = string.split(line, ',')
            if fields[4] == 'High Tide' or fields[4] == 'Low Tide':
                s = '%s %s' % (fields[1], fields[2])
                tt = time.strptime(s, '%Y.%m.%d %H:%M')
                ts = time.mktime(tt)
                ofields = string.split(fields[3], ' ')
                record = {}
                record['usUnits'] = weewx.US \
                    if ofields[1] == 'ft' else weewx.METRIC
                record['dateTime'] = int(now)
                record['ts'] = int(ts)
                record['hilo'] = hilo[fields[4]]
                record['offset'] = ofields[0]
                records.append(record)
        return records



class ForecastFileGenerator(FileGenerator):
    """Extend the standard file generator with forecasting variables."""

    def getCommonSearchList(self, archivedb, statsdb, timespan):
        searchList = super(ForecastFileGenerator, self).getCommonSearchList(archivedb, statsdb, timespan)
#        fdata = ForecastData()
#        searchList.append({'forecast', fdata})
        return searchList

#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2018 Maurizio Montel (dr-prodigy) <maurizio.montel@gmail.com>
# This file is part of hompi <https://github.com/dr-prodigy/hompi>.
#
# hompi is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# hompi is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with hompi.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import glob
import json
import codecs
import six.moves.urllib.request as request
import hashlib
import traceback
import datetime
import dateutil.parser

# GPIO import
try:
    import RPi.GPIO as GPIO
except ImportError:
    print('WARN: RPi.GPIO missing - loading stub library')
    import stubs.RPi.GPIO as GPIO

import config

if config.MODULE_TEMP and config.HEATING_GPIO:
    try:
        HEATING_GPIO = int(config.HEATING_GPIO)
    except Exception:
        print("WARN: config.HEATING_GPIO missing or wrong, defaulting to 17")
        HEATING_GPIO = 17

reader = codecs.getreader("utf-8")

# calculate API_KEY (MD5 hash)
m = hashlib.md5()
m.update(config.API_KEY.encode('utf-8'))
API_KEY = m.hexdigest().upper()
print('API_KEY: {}'.format(API_KEY))


class Sensors():
    METEO_URL = 'http://api.openweathermap.org/data/2.5/weather?' +\
                'q=[place]&units=metric&appid=6913ade53fd71e3c428b17a11807a6bf'
    APHORISM_URL = 'http://api.forismatic.com/api/1.0/?' +\
                   'method=getQuote&lang=en&format=json'
    device_file = ''

    def __init__(self):
        # DS18B20 thermometer
        # initialize GPIO modules
        os.system('sudo modprobe w1-gpio')
        os.system('sudo modprobe w1-therm')

        # initialize devices
        base_dir = '/sys/bus/w1/devices/'
        device_folders = glob.glob(base_dir + '28*')
        if device_folders:
            self.device_file = device_folders[0] + '/w1_slave'
        else:
            self.device_file = None

        # GPIO relay: BCM mode
        GPIO.setmode(GPIO.BCM)

        # relay control type
        if config.RELAY_HILOW_MODE:
            if config.MODULE_TEMP:
                GPIO.setup(HEATING_GPIO, GPIO.OUT)
            for sw in config.BUTTONS:
                GPIO.setup(sw[0], GPIO.OUT)

        # init heating to OFF
        self.set_heating(False)
        # init switches to OFF
        for sw in config.BUTTONS:
            self.set_switch(sw[0], False)

    # meteo status
    def get_meteo(self):
        meteo = None
        try:
            meteo = json.load(reader(request.urlopen(
                self.METEO_URL.replace('[place]', config.PLACE), timeout=5)))
            print(
               ('{} - Weather: {} - Temp.: {}° - Humidity: {}% Pressure: {} ' +
                'mbar - Wind: {} m/s').format(
                meteo['name'], meteo['weather'][0]['main'],
                meteo['main']['temp'],
                meteo['main']['humidity'],
                meteo['main']['pressure'],
                meteo['wind']['speed']))
        except request.URLError:
            print('WARNING: meteo not available.')
        except Exception:
            sys.stderr.write(traceback.format_exc())
        finally:
            return meteo

    # aphorisms
    def get_aphorism(self):
        aphorism = None
        try:
            req = request.Request(self.APHORISM_URL, None,
                                  {'User-Agent': 'Mozilla/5.0'})
            aphorism = json.load(reader(request.urlopen(req, timeout=5)))
            print(u'{} - {}'.format(
                aphorism['quoteText'], aphorism['quoteAuthor']).encode(
                'utf-8'))
        except request.URLError:
            print('WARNING: aphorism not available: skipped.')
        except Exception:
            # don't echo errors to stderr
            print(traceback.format_exc())
        finally:
            return aphorism

    # temperature sensor
    def _read_temp_raw(self):
        if self.device_file:
            try:
                f = open(self.device_file, 'r')
                lines = f.readlines()
                f.close()
                return lines
            except Exception:
                # in case of a temp sensor issue, return None
                return None
        else:
            return None

    def read_temp(self):
        lines = self._read_temp_raw()
        if lines:
            while lines[0].strip()[-3:] != 'YES':
                datetime.sleep(0.2)
                lines = self._read_temp_raw()
            equals_pos = lines[1].find('t=')
            if equals_pos != -1:
                temp_string = lines[1][equals_pos + 2:]
                temp_c = float(temp_string) / 1000.0
                return temp_c * config.TEMP_CORRECTION
        return None

    # heating relay management
    def set_heating(self, status):
        if config.MODULE_TEMP:
            print("*SENSOR* - HEATING={}".format(status))
            if config.RELAY_HILOW_MODE:
                GPIO.output(HEATING_GPIO, GPIO.LOW if status else GPIO.HIGH)
            else:
                GPIO.setup(HEATING_GPIO, GPIO.OUT if status else GPIO.IN)

    # switch relay management
    def set_switch(self, gpio, status):
            print("*SENSOR* - SWITCH({})={}".format(gpio, status))
            if config.RELAY_HILOW_MODE:
                GPIO.output(gpio, GPIO.LOW if status else GPIO.HIGH)
            else:
                GPIO.setup(gpio, GPIO.OUT if status else GPIO.IN)

    # MULTI-hompi
    # refresh hompi slave servers status
    def hompi_slaves_refresh(self, hompi_slaves):
        hompi_slaves.clear()
        for server in config.HOMPI_SERVERS:
            try:
                status = json.load(
                    reader(request.urlopen(
                        '{}/hompi/_get_status?api_key={}'.format(server,
                                                                 API_KEY),
                        timeout=2)))
                # avoid nested slaves
                del status['hompi_slaves']
                status['address'] = server
                # ignore servers updated more than 5 minutes ago (= dead hompi)
                if (datetime.datetime.now() - dateutil.parser.parse(
                        status['last_update'])).total_seconds() < 300:
                    hompi_slaves[status['id']] = status
                    print('{} server replied with status: saved.'.format(
                        status['id']))
            except request.URLError:
                print('WARNING: {} server not available.'.format(server))
            except Exception:
                sys.stderr.write(traceback.format_exc())

    # forward command to hompi slaves
    def hompi_slaves_forward_command(self, hompi_slaves, command):
        for slave_id, slave_data in hompi_slaves.items():
            if slave_id != config.HOMPI_ID:
                try:
                    command_json = json.dumps({'data': command})
                    request.urlopen(
                        '{}/hompi/_send_command/{}?api_key={}'.format(
                            slave_data['address'], request.quote(command_json),
                            API_KEY),
                        timeout=2)
                    print('Forwarded COMMAND: {} to: {}'.format(command,
                                                                slave_id))
                except request.URLError:
                    print(
                        'WARNING: {} server not available.'.format(slave_id))
                except Exception:
                    sys.stderr.write(traceback.format_exc())

    def cleanup(self):
        self.set_heating(False)
        for sw in config.BUTTONS:
            self.set_switch(sw[0], False)
        GPIO.cleanup()

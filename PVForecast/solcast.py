"""
Copyright (C) 2022    Stefan Eichenberger   se_misc ... hotmail.com

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

This is the main script to run a simulation of PVControl for one or multiple days. 
This script is typically called interactively on a performant machine. By default, 
config.ini in the local directory is the configuration file. But argument -c can
specify a different file.
"""

from pysolcast.rooftop import RooftopSite
from datetime          import datetime, timezone, timedelta
from astral            import LocationInfo
from astral.sun        import sun

import pandas as pd
import re
import sys
import pickle                                                                            # used for debugging only

from .forecast     import Forecast
from .dbrepository import DBRepository
from .influx       import InfluxRepo

class SolCast(Forecast):
    def __init__(self, config):
        """Initialize PVModel
        config      configparser object with section [SolCast]"""

        super().__init__()
        self.config        = config
        resource_id        = self.config['SolCast'].get('resource_id')
        resource_id_2      = self.config['SolCast'].get('resource_id_2', None)           # 2nd resource_id for split array installations
        api_key            = self.config['SolCast'].get('api_key')
        self._site         = RooftopSite(api_key, resource_id)
        if resource_id_2 is not None: self._site_2 = RooftopSite(api_key, resource_id_2)
        else:                         self._site_2 = None
        interval           = self.config['SolCast'].get('interval', '0').lower()         # set polling interval
        try:
            self._interval = int(interval)
        except:
            if interval   == 'late':   self._interval = -1                               # call often late,  at cost of neglecting early
            elif interval == 'early':  self._interval = -2                               # call often early, at cost of neglecting late
            elif interval == 'allday': self._interval = -3                               # call all day,     during night 4 times less
            else:                      self._interval =  0                               # call often over mid-day
        self._night_factor = 4                                                           # During night call APIs 4 times less
        self._db           = None                                                        # DBRepository object once DB is opened
        self._storeDB      = self.config['SolCast'].getboolean('storeDB', 0)             # ... store to DB
        self._storeInflux  = self.config['SolCast'].getboolean('storeInflux')            # ... store to Influx (one of the two must be true to make sense to get data from solcast)
        self._storeCSV     = self.config['SolCast'].getboolean('storeCSV')               # ... store to csv in storePath
        self.storePath     = self.config['SolCast'].get('storePath')
        self._force        = self.config['SolCast'].getboolean('force', False)           # force download - note that we are restricted in number of downloads/day
        self._apiCalls     = 50                                                          # max API calls per day
        if self._force:
            print("Warning --- SolCast download forced!!! Note limits in number of downloads/day!")
        self.SQLTable      = 'solcast'
        self.postDict      = None                                                        # dictionary to post to solcat

    def _doDownload(self):
        latitude       = self.config['SolCast'].getfloat('Latitude', 50.2)               # default describe Frankfurt, Germany
        longitude      = self.config['SolCast'].getfloat('Longitude', 8.7)
        location       = LocationInfo('na', 'na', 'UTC', latitude=latitude, longitude=longitude)
        now_utc        = datetime.now(timezone.utc)
        mySun          = sun(location.observer, date=now_utc)
        retVal         = False
        in_day         = (now_utc > mySun['sunrise'] and now_utc < mySun['sunset'])

        print(f"self._force={self._force}, in_day={in_day}, self._interval={self._interval}")
        if self._force or in_day or self._interval == -3:                                # storeDB enabled, SolCast enabled, daylight
            if self._storeDB or self._storeInflux:
                if self._storeDB:
                    self._db        = DBRepository(self.config)
                    self.last_issue = self._db.getLastIssueTime(self.SQLTable)
                    if self._storeInflux: self._influx = InfluxRepo(self.config)         # need to open Influx to later load data
                else:
                    self._influx    = InfluxRepo(self.config)
                    self.last_issue = self._influx.getLastIssueTime(self.SQLTable)
                print(f"now_utc={now_utc}, self.last_issue={self.last_issue}");
                delta_t             = round((now_utc - self.last_issue).total_seconds()/60)
                if self._force or self._interval > 0:                                    # we use an explicit calling interval
                    if self._force or delta_t > self._interval - 2:
                        retVal = True
                else:                                                                    # self._interval = 0: Choose optimal interval
                    optimal      = 15                                                    # optimal interval for single site / dual site setup
                    have         = self._apiCalls - 1                                    # keep one API call as reserve
                    day_period   = (mySun['sunset'] - mySun['sunrise']).total_seconds()/60
                    if self._site_2 is not None:
                        optimal  = optimal*2
                        have     = int(have/2)                                           # split-arrays need two calls per download
                    need         = int(int(day_period+1)/optimal)+1                      # number of 'optimal' minute intervals between sunrise and sunset
                    long         = need - have                                                                         # number of times where we can only call at longer intervals
                    if   self._interval ==  0 and ((now_utc - mySun['sunrise']).total_seconds()/60 < long*optimal or (mySun['sunset'] - now_utc).total_seconds()/60 < long*optimal):
                        interval = optimal*2
                    elif self._interval == -1 and (now_utc - mySun['sunrise']).total_seconds()/60 < long*optimal*2:    # focus on late,  neglect early
                        interval = optimal*2
                    elif self._interval == -2 and (mySun['sunset'] - now_utc).total_seconds()/60 < long*optimal*2:     # focus on early, neglect late
                        interval = optimal*2
                    elif self._interval == -3:
                        night_period = 1440 - day_period
                        interval = (night_period / self._night_factor + day_period) / have
                        if not in_day:
                            interval *= self._night_factor
                        print(f"have={have}, day_period={day_period}, night_period={night_period}, interval={interval}, delta_t={delta_t}, in_day={in_day}")
                    else:
                        interval = optimal
                    if delta_t > interval - 2:
                        retVal = True
                if retVal:
                    print("Message - downloading SolCast data at (UTC): " + str(now_utc))
            else:
                print("Warning --- getting SolCast data not supported without database storage enabled (storeDB or storeInflux)")
        return(retVal)

    def getSolCast(self):
        if (self._doDownload()):
            hours = self.config['SolCast'].getint('Hours', 168)                          # requires update of pysolcast
            try:
                forecasts_1 = self._getSolCast(self._site, {'hours':hours})
                if self._site_2 is not None: 
                    forecasts_2 = self._getSolCast(self._site_2, {'hours':hours})
            except Exception as e:
                print ("getSolCast: " + str(e))
                sys.exit(1)

            # --------- debugging begin
            #myFile    = open('./temp/forecasts_01', 'wb')                               # store forecast to file for later debugging runs 
            #pickle.dump(forecasts, myFile)
            #myFile.close()
            #
            # myFile      = open('./temp/forecasts_demo_02', 'rb')                       # load dummy solcast forecast for debugging
            # forecasts_1 = pickle.load(myFile)
            # forecasts_2 = forecasts_1
            # myFile.close()
            # --------- debugging end

            df                  = pd.DataFrame(forecasts_1['forecasts'])
            df                  = df.set_index('period_end')
            period              = df['period'][0]
            df.drop('period', axis=1, inplace=True)
            if self._site_2 is not None:
                cols            = list(df)
                df.columns      = [str(c) + '_1' for c in cols]
                df_2            = pd.DataFrame(forecasts_2['forecasts'])
                df_2            = df_2.set_index('period_end')
                df_2.drop('period', axis=1, inplace=True)
                df_2.columns    = [str(c) + '_2' for c in cols]
                df              = pd.merge(df, df_2, on='period_end', how='inner')
                for c in cols:
                    df[c]       = df[c + '_1'] + df[c + '_2']
            df.index.name       = 'PeriodEnd'
            self.DataTable      = df*1000                                                # convert kW to W
            issueTime           = (self.DataTable.index[0] - period).to_pydatetime()
            now_utc             = datetime.now(timezone.utc)
            if (now_utc - issueTime).total_seconds()/60 > 8:                             # we are more than 8min late
                issueTime       = issueTime + timedelta(0, 15*60)                        # add 15 min to IssueTime
            self.IssueTime      = str(issueTime)
            self.InfluxFields   = self.get_ParaNames()
            if self._storeDB: self._db.loadData(self)                                    # store data in repository, db was opened in self._doDownload()
            if self.config['SolCast'].getboolean('storeInflux'):
                self._influx.loadData(self)                                              # store data to Influx, client was opened in self._doDownload()
                if self.config['SolCast'].getboolean('post', 0):
                    power_fields  = ['power_field', 'power_field_2']
                    for field in power_fields:
                        if field == 'power_field_2': suffix = '_2'
                        else:                        suffix = ''
                        if (self.config.has_option('Influx', field)):
                            self.postDict = self._influx.getPostData(self, field)
                            if self.postDict is not None:
                                if self._storeDB:
                                    myPost           = Forecast()
                                    myPost.DataTable = pd.DataFrame.from_dict(self.postDict['measurements'])[['period_end', 'total_power']]
                                    myPost.DataTable['period_end'] = pd.to_datetime(myPost.DataTable['period_end'])
                                    myPost.DataTable.rename(columns={'period_end' : 'PeriodEnd'}, inplace=True)
                                    myPost.DataTable.set_index('PeriodEnd', inplace=True)
                                    myPost.IssueTime = self.IssueTime
                                    myPost.SQLTable  = self.SQLTable + '_post' + suffix
                                    self._db.loadData(myPost)
                                try:
                                    if suffix == '_2': self._site_2.post_measurements(self.postDict)
                                    else:              self._site.post_measurements(self.postDict)
                                except Exception as e:
                                    print ("getSolCast/post: " + str(e))
                            else:
                                print("Warning --- posting attempted without config file entry [Influx].[power_field]")
            if self._storeDB: 
                del self._storeDB
                self._storeDB = None
            if self._storeCSV:
                self.csvName  = 'solcast_' + self.IssueTime[:16].replace(' ', '_').replace(':', '-') + '.csv.gz'
                self.writeCSV()

    def _getSolCast(self, _site, hours):                                                 # pysolcast < 1.0.12 does not accept parameters hours
        result = None
        try:
            result = _site.get_forecasts_parsed(hours)
        except Exception:
            result = _site.get_forecasts_parsed()
        return result

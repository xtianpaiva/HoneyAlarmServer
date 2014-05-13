#!/usr/bin/python
## Alarm Server
## Supporting Envisalink 2DS/3
## Original version for DSC Written by donnyk+envisalink@gmail.com, lightly improved by leaberry@gmail.com
## Honeywell version adapted by matt.weinecke@gmail.com
##
## This code is under the terms of the GPL v3 license.


import asyncore, asynchat
import ConfigParser
import datetime
import os, socket, string, sys, httplib, urllib, urlparse, ssl
import StringIO, mimetools
import json
import hashlib
import time
import getopt


from envisalinkdefs import *


LOGTOFILE = False

class CodeError(Exception): pass

ALARMSTATE={'version' : 0.1}
MAXPARTITIONS=16
MAXZONES=128
MAXALARMUSERS=47


def getMessageType(code):
    return evl_ResponseTypes[code]

def alarmserver_logger(message, type = 0, level = 0):
    if LOGTOFILE:
        outfile.write(str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))+' '+message+'\n')
        outfile.flush()
    else:
        print (str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))+' '+message)
    

#currently supports pushover notifications, more to be added
#including email, text, etc.
#to be fixed!
def send_notification(config, message):
    if config.PUSHOVER_ENABLE == True:
        conn = httplib.HTTPSConnection("api.pushover.net:443")
        conn.request("POST", "/1/messages.json",
            urllib.urlencode({
            "token": "qo0nwMNdX56KJl0Avd4NHE2onO4Xff",
            "user": config.PUSHOVER_USERTOKEN,
            "message": str(message),
            }), { "Content-type": "application/x-www-form-urlencoded" })

class AlarmServerConfig():
    def __init__(self, configfile):

        self._config = ConfigParser.ConfigParser()
        self._config.read(configfile)

        self.LOGURLREQUESTS = self.read_config_var('alarmserver', 'logurlrequests', True, 'bool')
        self.HTTPSPORT = self.read_config_var('alarmserver', 'httpsport', 8111, 'int')
        self.CERTFILE = self.read_config_var('alarmserver', 'certfile', 'server.crt', 'str')
        self.KEYFILE = self.read_config_var('alarmserver', 'keyfile', 'server.key', 'str')
        self.MAXEVENTS = self.read_config_var('alarmserver', 'maxevents', 10, 'int')
        self.MAXALLEVENTS = self.read_config_var('alarmserver', 'maxallevents', 100, 'int')
        self.ENVISALINKHOST = self.read_config_var('envisalink', 'host', 'envisalink', 'str')
        self.ENVISALINKPORT = self.read_config_var('envisalink', 'port', 4025, 'int')
        self.ENVISALINKPASS = self.read_config_var('envisalink', 'pass', 'user', 'str')
        self.ENABLEPROXY = self.read_config_var('envisalink', 'enableproxy', True, 'bool')
        self.ENVISALINKPROXYPORT = self.read_config_var('envisalink', 'proxyport', self.ENVISALINKPORT, 'int')
        self.ENVISALINKPROXYPASS = self.read_config_var('envisalink', 'proxypass', self.ENVISALINKPASS, 'str')
        self.PUSHOVER_ENABLE = self.read_config_var('pushover', 'enable', False, 'bool')
        self.PUSHOVER_USERTOKEN = self.read_config_var('pushover', 'enable', False, 'bool')
        self.ALARMCODE = self.read_config_var('envisalink', 'alarmcode', 1111, 'int')
        self.EVENTTIMEAGO = self.read_config_var('alarmserver', 'eventtimeago', True, 'bool')
        self.LOGFILE = self.read_config_var('alarmserver', 'logfile', '', 'str')
        global LOGTOFILE
        if self.LOGFILE == '':
            LOGTOFILE = False
        else:
            LOGTOFILE = True

        self.PARTITIONNAMES={}
        for i in range(1, MAXPARTITIONS+1):
            self.PARTITIONNAMES[i]=self.read_config_var('alarmserver', 'partition'+str(i), False, 'str', True)

        self.ZONENAMES={}
        for i in range(1, MAXZONES+1):
            self.ZONENAMES[i]=self.read_config_var('alarmserver', 'zone'+str(i), False, 'str', True)

        self.ALARMUSERNAMES={}
        for i in range(1, MAXALARMUSERS+1):
            self.ALARMUSERNAMES[i]=self.read_config_var('alarmserver', 'user'+str(i), False, 'str', True)

        if self.PUSHOVER_USERTOKEN == False and self.PUSHOVER_ENABLE == True: self.PUSHOVER_ENABLE = False

    def defaulting(self, section, variable, default, quiet = False):
        if quiet == False:
            print('Config option '+ str(variable) + ' not set in ['+str(section)+'] defaulting to: \''+str(default)+'\'')

    def read_config_var(self, section, variable, default, type = 'str', quiet = False):
        try:
            if type == 'str':
                return self._config.get(section,variable)
            elif type == 'bool':
                return self._config.getboolean(section,variable)
            elif type == 'int':
                return int(self._config.get(section,variable))
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.defaulting(section, variable, default, quiet)
            return default

class HTTPChannel(asynchat.async_chat):
    def __init__(self, server, sock, addr):
        asynchat.async_chat.__init__(self, sock)
        self.server = server
        self.set_terminator("\r\n\r\n")
        self.header = None
        self.data = ""
        self.shutdown = 0

    def collect_incoming_data(self, data):
        self.data = self.data + data
        if len(self.data) > 16384:
        # limit the header size to prevent attacks
            self.shutdown = 1

    def found_terminator(self):
        if not self.header:
            # parse http header
            fp = StringIO.StringIO(self.data)
            request = string.split(fp.readline(), None, 2)
            if len(request) != 3:
                # badly formed request; just shut down
                self.shutdown = 1
            else:
                # parse message header
                self.header = mimetools.Message(fp)
                self.set_terminator("\r\n")
                self.server.handle_request(
                    self, request[0], request[1], self.header
                    )
                self.close_when_done()
            self.data = ""
        else:
            pass # ignore body data, for now

    def pushstatus(self, status, explanation="OK"):
        self.push("HTTP/1.0 %d %s\r\n" % (status, explanation))

    def pushok(self, content):
        self.pushstatus(200, "OK")
        self.push('Content-type: application/json\r\n')
        self.push('Expires: Sat, 26 Jul 1997 05:00:00 GMT\r\n')
        self.push('Last-Modified: '+ datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")+' GMT\r\n')
        self.push('Cache-Control: no-store, no-cache, must-revalidate\r\n' ) 
        self.push('Cache-Control: post-check=0, pre-check=0\r\n') 
        self.push('Pragma: no-cache\r\n' )
        self.push('\r\n')
        self.push(content)

    def pushfile(self, file):
        self.pushstatus(200, "OK")
        extension = os.path.splitext(file)[1]
        if extension == ".html":
            self.push("Content-type: text/html\r\n")
        elif extension == ".js":
            self.push("Content-type: text/javascript\r\n")
        elif extension == ".png":
            self.push("Content-type: image/png\r\n")
        elif extension == ".css":
            self.push("Content-type: text/css\r\n")
        self.push("\r\n")
        self.push_with_producer(push_FileProducer(sys.path[0] + os.sep + 'ext' + os.sep + file))

class EnvisalinkClient(asynchat.async_chat):
    def __init__(self, config):
        # Call parent class's __init__ method
        asynchat.async_chat.__init__(self)

        # Define some private instance variables
        self._buffer = []

        # Are we logged in?
        self._loggedin = False

        self._has_partition_state_changed = False

        # Set our terminator to \n
        self.set_terminator("\r\n")

        # Set config
        self._config = config

        # Reconnect delay
        self._retrydelay = 10

        self.do_connect()

    def do_connect(self, reconnect = False):
        # Create the socket and connect to the server
        if reconnect == True:
            alarmserver_logger('Connection failed, retrying in '+str(self._retrydelay)+ ' seconds')
            alarmserver_logger('resetting input buffer')
            self._buffer = []
            for i in range(0, self._retrydelay):
                time.sleep(1)

        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)

        self.connect((self._config.ENVISALINKHOST, self._config.ENVISALINKPORT))

    def collect_incoming_data(self, data):
        # Append incoming data to the buffer
        self._buffer.append(data)

    def found_terminator(self):
        line = "".join(self._buffer)
        self.handle_line(line)
        self._buffer = []

    def handle_connect(self):
        alarmserver_logger("Connected to %s:%i" % (self._config.ENVISALINKHOST, self._config.ENVISALINKPORT))

    def handle_close(self):
        self._loggedin = False
        self.close()
        alarmserver_logger("Disconnected from %s:%i" % (self._config.ENVISALINKHOST, self._config.ENVISALINKPORT))
        self.do_connect(True)

    def send_data(self,data):
        alarmserver_logger('TX > '+data)
        self.push(data)

    def send_envisalink_command(self, code, data):
        to_send = '^'+code+','+data+'$'
        self.send_data(to_send)

    def handle_line(self, input):
        if input != '':
 
            alarmserver_logger('----------------------------------------')
            alarmserver_logger('RX < ' + input)
            if input[0] in ("%","^"):
                #keep first sentinel char to tell difference between tpi and Envisalink command responses.  Drop the trailing $ sentinel.
                inputList = input[0:-1].split(',')
                code = inputList[0]
                data = ','.join(inputList[1:])
            else:
                #assume it is login info
                code = input
                data = ''
            
            
            try:
                handler = "handle_%s" % evl_ResponseTypes[code]['handler']
            except KeyError:
                alarmserver_logger('No handler defined for '+code+', skipping...')
                return

            try:
                handlerFunc = getattr(self, handler)
            except AttributeError:
                raise CodeError("Handler function doesn't exist")

            
            handlerFunc(data)
            alarmserver_logger('----------------------------------------')
 

    #envisalink event handlers, some events are unhandled.
    def handle_login(self, data):
        self.send_data(self._config.ENVISALINKPASS)

    def handle_login_success(self,data):
        self._loggedin = True
        alarmserver_logger('Password accepted, session created')

    def handle_login_failure(self, data):
        alarmserver_logger('Password is incorrect. Server is closing socket connection.')

    def handle_login_timeout(self,data):
        alarmserver_logger('Envisalink timed out waiting for password, whoops that should never happen. Server is closing socket connection')

    def handle_keypad_update(self,data):
        dataList = data.split(',')
        #make sure data is in format we expect, current TPI seems to send bad data every so ofen
        if len(dataList) !=5 or "%" in data:
            alarmserver_logger("Data format invalid from Envisalink, ignoring...")
            return

        partitionNumber = int(dataList[0])
        flags = IconLED_Flags()
        flags.asShort = int(dataList[1],16)
        userOrZone = dataList[2]
        beep = evl_Virtual_Keypad_How_To_Beep.get(dataList[3],'unknown')
        alpha = dataList[4]

 
        self.ensure_init_alarmstate(partitionNumber)
        ALARMSTATE['partition'][partitionNumber]['status'].update( {'alarm' : bool(flags.alarm), 'alarm_in_memory' : bool(flags.alarm_in_memory), 'armed_away' : bool(flags.armed_away),
                                                        'ac_present' : bool(flags.ac_present), 'armed_bypass' : bool(flags.bypass), 'chime' : bool(flags.chime),
                                                        'armed_zero_entry_delay' : bool(flags.armed_zero_entry_delay), 'alarm_fire_zone' : bool(flags.alarm_fire_zone),
                                                        'trouble' : bool(flags.system_trouble), 'ready' : bool(flags.ready), 'fire' : bool(flags.fire),
                                                        'armed_stay' : bool(flags.armed_stay),
                                                        'alpha' : alpha,  
                                                        'beep' : beep,
                                                        })
        
        #if we have never yet received a partition state changed event,  we need to compute the armed state ourselves.   Don't want to always do it here because we can't also
        #figure out if we are in entry/exit delay from here
        if not self._has_partition_state_changed:
            ALARMSTATE['partition'][partitionNumber]['status'].update( {'armed' : bool(flags.armed_away or flags.bypass or flags.armed_zero_entry_delay or flags.armed_stay)})

        alarmserver_logger(json.dumps(ALARMSTATE))


    def handle_zone_state_change(self,data):
        #Honeywell Panels or Envisalink currently does not seem to generate these events
        alarmserver_logger('zone state change handler not implemented yet')

    def handle_partition_state_change(self,data):
        self._has_partition_state_changed = True
        for currentIndex in range(0,8):
            partitionState = data[currentIndex*2:(currentIndex*2)+2]
            if partitionState != '00':
                partitionNumber = currentIndex + 1
                self.ensure_init_alarmstate(partitionNumber)
                previouslyArmed = ALARMSTATE['partition'][partitionNumber]['status'].get('armed',False)
                ALARMSTATE['partition'][partitionNumber]['status'].update({'exit_delay' : bool(partitionState == '07' and not previouslyArmed), 
                                                                           'entry_delay' : bool (partitionState == '07' and previouslyArmed),
                                                                           'armed' : bool (partitionState == '04' or partitionState == '05' or partitionState == '06')} )

                alarmserver_logger('Parition ' + str(partitionNumber) + ' is in state ' + evl_Partition_Status_Codes[partitionState])
                alarmserver_logger(json.dumps(ALARMSTATE))

    def handle_realtime_cid_event(self,data):
        qualifier = evl_CID_Qualifiers[int(data[0])]
        cidEvent = evl_CID_Events[int(data[1:4])]
        partition = data[4:6]
        zoneOrUser = data[6:9]


        alarmserver_logger('Event Type is '+qualifier)
        alarmserver_logger('CID Type is '+cidEvent['type'])
        alarmserver_logger('CID Description is '+cidEvent['label'])
        alarmserver_logger('Partition is '+partition)
        alarmserver_logger(cidEvent['type'] + ' value is ' + zoneOrUser)


    def ensure_init_alarmstate(self,partitionNumber):
        if not 'partition' in ALARMSTATE: ALARMSTATE['partition']={'lastevents' : []}
        if partitionNumber in self._config.PARTITIONNAMES:
            if not partitionNumber in ALARMSTATE['partition']: ALARMSTATE['partition'][partitionNumber] = {'name' : self._config.PARTITIONNAMES[partitionNumber]}
        else:
            if not partitionNumber in ALARMSTATE['partition']: ALARMSTATE['partition'][partitionNumber] = {}
        if not 'lastevents' in ALARMSTATE['partition'][partitionNumber]: ALARMSTATE['partition'][partitionNumber]['lastevents'] = []
        if not 'status' in ALARMSTATE['partition'][partitionNumber]: ALARMSTATE['partition'][partitionNumber]['status'] = {}

class push_FileProducer:
    # a producer which reads data from a file object

    def __init__(self, file):
        self.file = open(file, "rb")

    def more(self):
        if self.file:
            data = self.file.read(2048)
            if data:
                return data
            self.file = None
        return ""

class AlarmServer(asyncore.dispatcher):
    def __init__(self, config):
        # Call parent class's __init__ method
        asyncore.dispatcher.__init__(self)

        # Create Envisalink client object
        self._envisalinkclient = EnvisalinkClient(config)

        #Store config
        self._config = config

        # Create socket and listen on it
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.bind(("", config.HTTPSPORT))
        self.listen(5)

    def handle_accept(self):
        # Accept the connection
        conn, addr = self.accept()
        if (config.LOGURLREQUESTS):
            alarmserver_logger('Incoming web connection from %s' % repr(addr))

        try:
            HTTPChannel(self, ssl.wrap_socket(conn, server_side=True, certfile=config.CERTFILE, keyfile=config.KEYFILE, ssl_version=ssl.PROTOCOL_TLSv1), addr)
        except ssl.SSLError as e:
            alarmserver_logger("SSL error({0}): {1}".format(e.errno, e.strerror))
            return

    def handle_request(self, channel, method, request, header):
        if (config.LOGURLREQUESTS):
            alarmserver_logger('Web request: '+str(method)+' '+str(request))

        query = urlparse.urlparse(request)
        query_array = urlparse.parse_qs(query.query, True)
        if 'alarmcode' in query_array:
            alarmcode = str(query_array['alarmcode'][0])
        else:
            alarmcode = str(self._config.ALARMCODE)

        if query.path == '/':
            channel.pushfile('index.html');
        elif query.path == '/api':
            channel.pushok(json.dumps(ALARMSTATE))
        elif query.path == '/api/alarm/arm':
            self._envisalinkclient.send_data(alarmcode+'2')
            channel.pushok(json.dumps({'response' : 'Arm command sent to Envisalink.'}))
        elif query.path == '/api/alarm/stayarm':
            self._envisalinkclient.send_data(alarmcode+'3')
            channel.pushok(json.dumps({'response' : 'Arm Home command sent to Envisalink.'}))
        elif query.path == '/api/alarm/armwithcode':
            self._envisalinkclient.send_data(str(query_array['alarmcode'][0])+'2')
            channel.pushok(json.dumps({'response' : 'Arm With Code command sent to Envisalink.'}))
        elif query.path == '/api/pgm':
            channel.pushok(json.dumps({'response' : 'Request to trigger PGM'}))
            #self._envisalinkclient.send_command('020', '1' + str(query_array['pgmnum'][0]))
            #self._envisalinkclient.send_command('071', '1' + "*7" + str(query_array['pgmnum'][0]))
            #time.sleep(1)
            #self._envisalinkclient.send_command('071', '1' + str(query_array['alarmcode'][0]))
        elif query.path == '/api/alarm/disarm':
            self._envisalinkclient.send_data(alarmcode+'1')
            channel.pushok(json.dumps({'response' : 'Disarm command sent to Envisalink.'}))
        elif query.path == '/api/refresh':
            channel.pushok(json.dumps({'response' : 'Request to refresh data received'}))
            #self._envisalinkclient.send_command('001', '')
        elif query.path == '/api/config/eventtimeago':
            channel.pushok(json.dumps({'eventtimeago' : str(self._config.EVENTTIMEAGO)}))
        elif query.path == '/img/glyphicons-halflings.png':
            channel.pushfile('glyphicons-halflings.png')
        elif query.path == '/img/glyphicons-halflings-white.png':
            channel.pushfile('glyphicons-halflings-white.png')
        elif query.path == '/favicon.ico':
            channel.pushfile('favicon.ico')
        else:
            if len(query.path.split('/')) == 2:
                try:
                    with open(sys.path[0] + os.sep + 'ext' + os.sep + query.path.split('/')[1]) as f:
                        f.close()
                        channel.pushfile(query.path.split('/')[1])
                except IOError as e:
                    print "I/O error({0}): {1}".format(e.errno, e.strerror)
                    channel.pushstatus(404, "Not found")
                    channel.push("Content-type: text/html\r\n")
                    channel.push("File not found")
                    channel.push("\r\n")
            else:
                if (config.LOGURLREQUESTS):
                    alarmserver_logger("Invalid file requested")

                channel.pushstatus(404, "Not found")
                channel.push("Content-type: text/html\r\n")
                channel.push("\r\n")


def usage():
    print 'Usage: '+sys.argv[0]+' -c <configfile>'

def main(argv):
    try:
      opts, args = getopt.getopt(argv, "hc:", ["help", "config="])
    except getopt.GetoptError:
        usage()
        sys.exit(2)
    for opt, arg in opts:
        if opt in ("-h", "--help"):
            usage()
            sys.exit()
        elif opt in ("-c", "--config"):
            global conffile
            conffile = arg


if __name__=="__main__":
    conffile='alarmserver.cfg'
    main(sys.argv[1:])
    print('Using configuration file %s' % conffile)
    config = AlarmServerConfig(conffile)
    if LOGTOFILE:
        outfile=open(config.LOGFILE,'a')
        print ('Writing logfile to %s' % config.LOGFILE)

    alarmserver_logger('Alarm Server Starting')
    alarmserver_logger('Currently Supporting Envisalink 2DS/3 only')
    alarmserver_logger('Tested on a Honeywell Vista 15p + EVL-3')


    server = AlarmServer(config)
 
    try:
        while True:
            asyncore.loop(timeout=2, count=1)
            # insert scheduling code here.
    except KeyboardInterrupt:
        print "Crtl+C pressed. Shutting down."
        alarmserver_logger('Shutting down from Ctrl+C')
        if LOGTOFILE:
            outfile.close()
        
        server.shutdown(socket.SHUT_RDWR) 
        server.close() 
        sys.exit()
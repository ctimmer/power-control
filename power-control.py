#
################################################################################
# The MIT License (MIT)
#
# Copyright (c) 2022 Curt Timmerman
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
################################################################################
#
# title           :power-control.py
# description     :Power level controller
# author          :Curt Timmerman
# date            :20211031
# version         :0.1
# notes           :
# python_version  :3.*
#
################################################################################
#

import machine
import gc
import os
import utime as time

import network
import usocket as socket

import ujson as json
import ure
#import re

from machine import Pin, SPI

import st7789
import vga1_16x32 as font

from oled7segment import *

#import NotoSansMono_32 as font

from poll_looper import PollLooper

MACHINE_FREQ = 240000000
WIDTH = const(240)
HEIGHT = const(135)
ROTATION = const(1)

BLACK = st7789.BLACK
BLUE = st7789.BLUE
CYAN = st7789.CYAN
GREEN = st7789.GREEN
MAGENTA = st7789.MAGENTA
ORANGE = st7789.color565(255,165,0)
RED = st7789.RED
WHITE = st7789.WHITE
YELLOW = st7789.YELLOW

BG_COLOR = BLACK
COLOR = WHITE

my_hostname = "PowerControlOne"
my_device_id = "SmokerOne"

MINIMUM_PULSE_WIDTH_MS = 2000

INITIAL_POWER_LEVEL = 0.0

UDP_PORT = 5010
WEB_PORT = 5010

STANDBY_TIMEOUT_SECONDS = 60 # 5 min
#STANDBY_TIMEOUT_SECONDS = 30000 # 500 min - testing
STANDBY_POWER_LEVEL = 20.0

SHUTDOWN_HOURS = 24
SHUTDOWN_MINUTES = 0
SHUTDOWN_SECONDS = 0

WATCHDOG_TIMEOUT_MS = 10000     # 10 seconds

POWER_CONTROL_PIN = 2       # Randomly selected TBD

#---------------------------------------------------------------------------
# GetCommand
#   Example UDP command:
#     {
#     "jsonrpc": "2.0",                  # required but not used
#     "method": "set_power_level",       # required
#     "params": {"power_level": 42.2},   # required
#     "id": <INT>                        # optional
#     }
#     No result is returned
#
#   Example WEB query string (GET):
#     'GET /?power_level=42.2 ...
#     Note: This process may exceed the poll interval
#
#---------------------------------------------------------------------------
class GetCommand :

    def __init__(self,
                 poller ,
                 initial_power_level = 0.0 ,
                 udp_port = 5010 ,
                 web_port = 5010) :
        #print ("GetCommand: init")       
        self.poller = poller
        
        #---- UDP interface
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.my_ip = network.WLAN().ifconfig()[0]
        print (network.WLAN().ifconfig())
        self.address = socket.getaddrinfo(self.my_ip, udp_port)[0][-1]
        self.address = ("", udp_port)
        self.s.bind(self.address)
        self.s.settimeout(0)
        self.power_settings \
            = self.poller.message_set ("powercontrol",
                                            {"power_level": initial_power_level ,
                                            "last_update_ms": poller.get_current_time_ms ()})
        #---- Web server
        self.web_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.web_socket.settimeout (0)
        self.web_socket.bind(('0.0.0.0', web_port))
        self.web_socket.listen(5)
        self.html_header = 'HTTP/1.1 200 OK\n' \
                            + 'Content-Type: text/html\n' \
                            + 'Connection: close\n\n'
        
        self.poll_udp = True          # Alternate between UPD and WEB input

    def poll_it (self) :
        #print ("GetCommand: poll_it")
        if self.poll_udp :
            self.poll_udp = False
            #---- UDP input
            while True :
                try :
                    mess_address = self.s.recvfrom (2000)
                    #print ("GetC:", mess_address)
                    message = mess_address[0]
                    address_port = mess_address [1]
                    request_json = message.decode ()
                    request_dict = json.loads (request_json)
                    #print ("Cmd:", request_dict)
                    self.process_request (request_dict)
                except OSError :
                    #print ("GetC: no data")
                    break
        else :
            self.poll_udp = True
            #---- Web input
            #print ("Web Input")
            #while True :
            conn = False
            try :
                conn, addr = self.web_socket.accept()
                #print('Got a connection from %s' % str(addr))
                request = conn.recv(4096)
                query_params = self.qs_parse (request)
                #print (query_params)
                if query_params is None :
                    return                 # Probably a request for a file
                if query_params['param_count'] > 0 :
                    self.set_power_level (query_params)
                conn.sendall (self.html_header
                                + self.build_html (self.power_settings['power_level']))
            except OSError :
                #print ("Web: no data")
                pass
            finally :
                if conn :
                    conn.close ()

    def qs_parse(self, request) :
        parameters = {'param_count' : 0}
        #print ("qs_parse:", str(request))
        pattern = '^GET\s+/(\S+)'
        qs_match = ure.search (pattern, request)    # get the query string
        if qs_match is None :
            return parameters                   # probably 1st request
        qs = qs_match.group(1) \
             .decode("utf-8") \
             .replace("+", " ") \
             .replace("%3F", "?") \
             .replace("%21", "!")
        #print (qs)
        if len (qs) <= 0 :
            return None                         # empty query
        if qs[0] == "?" :
            qs = qs[1:]
        else :
            return None                         # query string missing
        #print (qs)
        ampersandSplit = qs.split("&")          # split id/value pairs
        for element in ampersandSplit:          # build id/value dictionary
            equalSplit = element.split("=")
            if len (equalSplit) < 2 :
                equalSplit.append ('')          # missing '='
            param_id = equalSplit[0].replace("+", " ").replace("%3F", "?").replace("%21", "!")
            param_val = equalSplit[1].replace("+", " ").replace("%3F", "?").replace("%21", "!")
            parameters[param_id] = param_val
            parameters ['param_count'] += 1
        return parameters                       # return query string dict

    def build_html (self, power_level='') :
        return '''<html>
<head>
<title>''' + my_device_id + ''' Web Server</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
html
  {font-family: Helvetica;
  display:inline-block;
  margin: 0px auto;
  text-align: center;}
h1
  {color: #0F3376;
  padding: 2vh;}
p
  {font-size: 1.5rem;}
.button
  {display: inline-block;
  background-color: #e7bd3b;
  border: none; 
  border-radius: 4px;
  color: white110.0;
  padding: 16px 40px;
  text-decoration: none;
  font-size: 30px;
  margin: 2px;
  cursor: pointer;}
.button2
  {background-color: #4286f4;}
</style>
</head>
<body>
<h1>''' + my_device_id + ''' Web Server</h1> 
<form>
<p>
Power Level<input name="power_level" type="text" value="''' \
    + "{:.1f}".format (power_level) + '''"/>
</p>
<p>
<input type="submit" value="Update" />
</p>
</form>
</body></html>'''

    def process_request (self, request) :
        #print ("request:", request)
        if not "jsonrpc" in request :
            print ("request: 'jsonrpc' missing")
            return
        if not "method" in request :
            print ("request: 'method' missing")
            return
        if not "params" in request :
            print ("request: 'params' missing")
            return
        if request["method"] == "set_power_level" :
            result = self.set_power_level (request["params"])
        elif request["method"] == "pid_update" :
            rquest ["params"]["temperature_update"] = \
                "current_temperature" in request ["params"]
            self.poller.message_set ("pid_settings", request["params"])
        elif request["method"] == "shutdown" :
            poller.shutdown ()

    def set_power_level (self, params) :
        #print ("set_power_level:", params)
        if not "power_level" in params :
            return
        try :
            #print (new_power_level)
            new_power_level = round (float (params ['power_level']), 1)
            self.poller.message_set ("powercontrol",
                                          {"power_level": new_power_level})
        except :
            return
        #self.poller.message_set ("powercontrol",
                                      #{"power_level": new_power_level ,
                                       #"last_update_ms" : poller.get_current_time_ms ()
                                       #})

    def shutdown (self) :
        self.s.close ()
        self.web_socket.close ()

# end GetCommand

#---------------------------------------------------------------------------
# PIDControl - 
#---------------------------------------------------------------------------
class PIDControl :
    
    import PID

    def __init__(self,
                 poller ,
                 P = 0.2 ,
                 I = 0.0 ,
                 D = 0.0 ,
                 set_point = 0.0 ,
                 current_time = None
                 ) :
        #print ("PIDControl: init")
        
        self.poller = poller             # save poller object
        self.last_update_ms = poller.get_current_time_ms ()
        self.pid = None
#pid.SetPoint=225.0
#pid.setSampleTime(0.01)
        self.current_time = current_time
        self.current_temperature = 0.0
        self.pid_settings \
            = self.poller.message_set ("pid_control",
                                            {"power_level": 0 ,
                                             "P" : P ,
                                             "I" : I ,
                                             "D" : D ,
                                             "set_point" : set_point ,
                                             "current_temperature" : self.current_temperature ,
                                             "temperature_update" : False})
                                             #"last_update_ms": poller.get_current_time_ms ()})

    def poll_it (self) :
        #print ("PIDControl: poll_it")
        if self.last_update_ms == self.pid_settings["last_update_ms"] :
            return                           # No change
        self.last_update_ms = self.pid_settings["last_update_ms"]
        if self.pid is None :                # First time
            print ("pid: initialize")
            self.pid = PID.PID (P = self.pid_settings ["P"] ,
                                I = self.pid_settings ["I"] ,
                                D = self.pid_settings ["D"])
            self.pid.SetPoint = self.pid_settings ["set_point"]
        if not self.pid_settings ["temperature_update"] :
            return
        self.pid.update (self.pid_settings ["current_temperature"])
        print ("pid.output:", self.pid.output)
        power_level = round (max (min (self.pid.output, 100.0), 0.0), 1)
        self.poller.message_set ("powercontrol",
                                     {"power_level" : power_level})

    def shutdown (self) :
        print ("PIDControl: shutdown")
        #---- Shutdown code goes here

# end PIDControl

#---------------------------------------------------------------------------
# PowerControl
#---------------------------------------------------------------------------
class PowerControl :

    def __init__(self ,
                 poller ,
                 standby_timeout_seconds = STANDBY_TIMEOUT_SECONDS ,
                 standby_power_level = STANDBY_POWER_LEVEL ,
                 minimum_pulse_ms = MINIMUM_PULSE_WIDTH_MS) :
        self.poller = poller
        self.power_control_pin = machine.Pin (POWER_CONTROL_PIN, machine.Pin.OUT)
        self.power_level = 50.0    # Should be zero
        self.minimum_pulse_ms = minimum_pulse_ms
        self.standby = False
        self.standby_timeout = poller.seconds_to_ms (standby_timeout_seconds)
        self.standby_power_level = standby_power_level
        #
        #---- display globals
        self.bg_color = BG_COLOR
        self.color = WHITE
        #
        #---- headings
        self.power_heading_display = {
            "xpos" : 116 ,
            "ypos" : 0 ,
            "color" : YELLOW ,
            "bg_color" : self.bg_color ,
            "text" : "Pwr Lvl"
            }
        display.text(
            font,
            self.power_heading_display["text"] ,       # Heading
            self.power_heading_display["xpos"] ,
            self.power_heading_display["ypos"] ,
            self.power_heading_display["color"] ,      # char color
            self.power_heading_display["bg_color"]     # background color
            )
        self.heading_1_display = {
            "xpos" : 0 ,
            "ypos" : 34 ,
            "color" : CYAN ,
            "bg_color" : self.bg_color ,
            "text" : "Power"
            }
        display.text(
            font,
            self.heading_1_display["text"] ,       # Heading
            self.heading_1_display["xpos"] ,
            self.heading_1_display["ypos"] ,
            self.heading_1_display["color"] ,      # char color
            self.heading_1_display["bg_color"]     # background color
            )
        self.heading_2_display = {
            "xpos" : 0 ,
            "ypos" : 62 ,
            "color" : CYAN ,
            "bg_color" : self.bg_color ,
            "text" : "Contrl"
            }
        display.text(
            font,
            self.heading_2_display["text"] ,       # Heading
            self.heading_2_display["xpos"] ,
            self.heading_2_display["ypos"] ,
            self.heading_2_display["color"] ,      # char color
            self.heading_2_display["bg_color"]     # background color
            )
        #
        #---- power level display set up
        self.power_level_display = {
            "xpos" : 110 ,
            "ypos" : 36 ,
            "color" : BLUE ,
            "bg_color" : self.bg_color
            }
        self.power_level_display["display"] = OLED7Segment (display,
                                                            color=self.power_level_display["color"])
        self.power_level_display["display"].set_parameters (digit_size="L" ,
                                                            v_segment_length=18 ,
                                                            spacing=10 ,
                                                            bold=True)
        #
        #---- power on display set up
        self.power_on_display = {
            "xpos" : 160 ,
            "ypos" : 100 ,
            "size" : 20 ,
            "bg_color" : self.bg_color ,
            "color" : COLOR ,
            "power_on_color" : RED ,
            "power_off_color" : WHITE
            }
        display.text(
            font,
            "Pow",                                # label
            (self.power_on_display["xpos"] + self.power_on_display["size"] + 4) ,
            self.power_on_display["ypos"] ,
            self.power_on_display["color"] ,      # char color
            self.power_on_display["bg_color"]     # background color
            )
        #
        #---- standby display set up
        self.standby_display = {
            "xpos" : 0 ,
            "ypos" : 100 ,
            "text" : "STANDBY" ,
            "bg_color" : YELLOW ,
            "color" : RED 
            }
        #
        #---- initial power settings
        self.set_standby_off ()
        self.set_power_off ()
        self.last_update_ms = poller.get_current_time_ms ()
        self.power_settings \
            = self.poller.message_set ("powercontrol",
                                            {"power_level": 0 ,
                                            "last_update_ms": poller.get_current_time_ms ()})
        self.new_power_level (self.power_level)

    def poll_it (self) :
        #print ("PowerControl: poll_it")
        if self.last_update_ms != self.power_settings["last_update_ms"] :
            self.last_update_ms = self.power_settings["last_update_ms"]
            self.set_standby_off ()
            #print (self.power_settings["power_level"], " ", self.power_level)
            if self.power_settings["power_level"] != self.power_level :
                self.new_power_level (self.power_settings["power_level"])
            return

        # test for timeout here
        current_time_ms = poller.get_current_time_ms ()
        #print (self.last_update_ms, " ", current_time_ms)
        #print ( time.ticks_diff (current_time_ms, self.last_update_ms))
        if not self.standby :
            if self.power_level > self.standby_power_level :
                if time.ticks_diff (current_time_ms, self.last_update_ms) \
                       > self.standby_timeout :
                    self.set_standby_on ()
                    #self.set_power_off ()
                    return

        if poller.active_now (self.change_ms) :
            #print ("power on/off")
            if self.power_on :
                if self.off_ms > 0 :
                    self.set_power_off ()
                    self.change_ms = self.poller.active_next_ms (self.off_ms)
            else :
                if self.on_ms > 0 :
                    self.set_power_on ()
                    self.change_ms = self.poller.active_next_ms (self.on_ms)

    def set_standby_on (self) :
        self.standby = True
        display.text(
            font,
            self.standby_display["text"] ,        # Heading
            self.standby_display["xpos"] ,
            self.standby_display["ypos"] ,
            self.standby_display["color"] ,      # char color
            self.standby_display["bg_color"]     # background color
            )
        #self.power_settings["power_level"] = self.standby_power_level
        #print ("===============", self.standby_power_level)
        self.new_power_level (self.standby_power_level)
    def set_standby_off (self) :
        if not self.standby :
            return
        self.standby = False
        display.text(
            font,
            "         " ,                         # Clear standby
            self.standby_display["xpos"] ,
            self.standby_display["ypos"] ,
            self.bg_color ,                       # char color
            self.bg_color                         # background color
            )
    
    def new_power_level (self, power_level) :
        #print ("new_power_level: ",
               #"New:" + "{:3.2f}".format (power_level) ,
               #" Old:" + "{:3.2f}".format (self.power_level)
               #)
        power_increase = power_level > self.power_level
        #self.power_settings["power_level"] = power_level
        self.power_level = power_level
        if power_level > 99.0 :         # Always on
            self.on_ms = 999999
            self.off_ms = 0
        elif power_level >= 50.0 :      # On > Off
            self.on_ms = int ((self.minimum_pulse_ms / ((100.0 - self.power_level) * 0.01))) \
                                - self.minimum_pulse_ms
            self.off_ms = self.minimum_pulse_ms
        elif power_level >= 1.0 :       # Off > On
            self.on_ms = self.minimum_pulse_ms
            self.off_ms = int ((self.minimum_pulse_ms / (self.power_level * 0.01))) \
                                - self.minimum_pulse_ms
        else :                               # Always off
            self.on_ms = 0
            self.off_ms = 999999
        if power_increase :
            #print ("increase")
            self.change_ms = self.poller.active_next_ms (self.on_ms)
            self.set_power_on ()
        else :
            #print ("decrease")
            self.change_ms = self.poller.active_next_ms (self.off_ms)
            self.set_power_off ()
        self.display_power_level ()
        #print ("On:", self.on_ms, "Off:", self.off_ms)
        
    def display_power_level (self) :
        display.fill_rect (self.power_level_display["xpos"] ,
                            self.power_level_display["ypos"] ,
                            122 ,
                            54 ,
                            #RED)
                            self.power_level_display["bg_color"])
        self.power_level_display["display"].display_string (self.power_level_display["xpos"] ,
                                                           self.power_level_display["ypos"] ,
                                                           "{:3d}".format (int (self.power_level)))

    def set_power_on (self) :
        self.power_control_pin.on ()
        self.power_on = True
        display.fill_rect (self.power_on_display["xpos"] ,
                            self.power_on_display["ypos"] + 3 ,
                            self.power_on_display["size"] ,
                            self.power_on_display["size"] ,
                            self.power_on_display["power_on_color"])
    def set_power_off (self) :
        self.power_control_pin.off ()
        self.power_on = False
        display.fill_rect (self.power_on_display["xpos"] ,
                            self.power_on_display["ypos"] + 3 ,
                            self.power_on_display["size"] ,
                            self.power_on_display["size"] ,
                            self.power_on_display["power_off_color"])

    def shutdown (self) :
        #print ("PowerControl: set power off")
        self.new_power_level (0.0)
        display.text(
            font,
            "Power Off" ,                        # power off text
            self.standby_display["xpos"] ,
            self.standby_display["ypos"] ,
            self.standby_display["color"] ,      # char color
            self.standby_display["bg_color"]     # background color
            )

# end PowerControl #

#---------------------------------------------------------------------------
# PollIndicator - Blink region on disply to show poll activity
#---------------------------------------------------------------------------
class PollIndicator:

    def __init__(self,
                 poller ,
                 xpos = 0 ,
                 ypos = 0 ,
                 color = WHITE) :
        #print ("PollIndicator: init")
        self.poller = poller
        self.display_xpos = xpos
        self.display_ypos = ypos
        self.bg_color = BG_COLOR
        self.color = WHITE
        self.poll_color_1 = GREEN
        self.poll_color_2 = ORANGE
        display.text(
            font,
            "Run" ,
            (self.display_xpos + 24) ,
            self.display_ypos , #random.randint(0, row_max),
            self.color ,      # char color
            self.bg_color     # background color
            )
        self.size = 10
        self.width =  2
        self.color = color
        self.active_interval_ms = 300       # activity blink milliseconds
        #self.active_next_ms = self.poller.active_next_ms (self.active_interval_ms)
        self.active_next_ms = self.poller.active_next_ms (0)
        self.blink_color_1 = self.poll_color_1
        self.blink_color_2 = self.poll_color_2
        self.poll_toggle = False
        self.indicator_xpos = self.display_xpos + 5
        self.indicator_ypos = self.display_ypos + 10
        self.indicator_cycle = 0
        display.fill_rect (self.display_xpos ,
                            self.display_ypos ,
                            self.size ,
                            self.width ,
                            self.bg_color)
        self.poll_it ()

    def poll_it (self) :
        #print ("PollIndicator: poll_it")
        if not poller.active_now (self.active_next_ms) :
            return
        #print ("PollIndicator: poll_it: change")
        self.active_next_ms = self.poller.active_next_ms (self.active_interval_ms)
        if self.indicator_cycle == 1 :
            self.indicator_cycle = 2
            self.left_segment (self.bg_color)
            self.top_segment (self.color)
            self.right_segment (self.color)
        elif self.indicator_cycle == 2 :
            self.indicator_cycle = 3
            self.top_segment (self.bg_color)
            self.right_segment (self.color)
            self.bottom_segment (self.color)
        elif self.indicator_cycle == 3 :
            self.indicator_cycle = 4
            self.right_segment (self.bg_color)
            self.bottom_segment (self.color)
            self.left_segment (self.color)
        elif self.indicator_cycle == 4 :
            self.indicator_cycle = 1
            self.bottom_segment (self.bg_color)
            self.left_segment (self.color)
            self.top_segment (self.color)
        else :
            self.indicator_cycle = 1
            self.top_segment (self.color)
            self.right_segment (self.color)
            self.bottom_segment (self.color)
            self.left_segment (self.color)
        #print ("mem_free:", gc.mem_free())
        #if gc.mem_free() < 50000 :
            #gc.collect()

    def top_segment (self, color) :
        display.fill_rect (self.indicator_xpos ,
                            self.indicator_ypos ,
                            self.size ,
                            self.width ,
                            color)
    def right_segment (self, color) :
        display.fill_rect (self.indicator_xpos + (self.size - self.width) ,
                            self.indicator_ypos ,
                            self.width ,
                            self.size ,
                            color)
    def bottom_segment (self, color) :
        display.fill_rect (self.indicator_xpos ,
                            self.indicator_ypos + (self.size - self.width) ,
                            self.size ,
                            self.width ,
                            color)
    def left_segment (self, color) :
        display.fill_rect (self.indicator_xpos ,
                            self.indicator_ypos ,
                            self.width ,
                            self.size ,
                            color)

    def shutdown (self) :
        self.top_segment (RED)
        self.right_segment (RED)
        self.bottom_segment (RED)
        self.left_segment (RED)
        
    def poll_it_alt (self) :
        #print ("PollIndicator: poll_it")
        if not poller.active_now (self.active_next_ms) :
            return
        #print ("PollIndicator: poll_it: change")
        self.active_next_ms = self.poller.active_next_ms (self.active_interval_ms)
        if self.poll_toggle :         # toggle led
            self.blink_color_1 = self.poll_color_1
            self.blink_color_2 = self.poll_color_2
        else :
            self.blink_color_1 = self.poll_color_2
            self.blink_color_2 = self.poll_color_1
        self.poll_toggle = not self.poll_toggle       # Toggle 
        display.fill_rect (self.display_xpos ,
                            self.display_ypos + 6 ,
                            self.size ,
                            self.size ,
                            self.blink_color_1)
        display.fill_rect (self.display_xpos + 4 ,
                            self.display_ypos + 10 ,
                            self.size - 8 ,
                            self.size - 8 ,
                            self.blink_color_2)

# end PollIndicator #

#---------------------------------------------------------------------------
# ShutdownTimer - Blink region on disply to show poll activity
#---------------------------------------------------------------------------
class ShutdownTimer:

    def __init__(self,
                 poller ,
                 hours = SHUTDOWN_HOURS ,
                 minutes = SHUTDOWN_MINUTES ,
                 seconds = SHUTDOWN_SECONDS) :
        self.run_ms = poller.hours_to_ms (hours) \
                           + poller.minutes_to_ms (minutes) \
                           + poller.seconds_to_ms (seconds)
        self.start_time_ms = poller.get_current_time_ms ()
        self.run_time_ms = self.start_time_ms
        self.last_time_ms = self.start_time_ms
        self.stop_time_ms = self.start_time_ms + self.run_ms

    def poll_it (self) :
        if self.run_ms <= 0 :
            return                   # Not set - exit
        current_time_ms = poller.get_current_time_ms ()
        self.run_time_ms += time.ticks_diff (current_time_ms, self.last_time_ms)
        self.last_time_ms = current_time_ms
        if self.run_time_ms >= self.stop_time_ms :
            poller.shutdown ()

    def shutdown (self) :
        pass

# end ShutdownTimer #

#---------------------------------------------------------------------------
# Watchdog
#---------------------------------------------------------------------------
class Watchdog:

    def __init__(self ,
                 wd_timeout_ms = WATCHDOG_TIMEOUT_MS) :
        #print ("Watchdog: init")
        self.wdt = machine.WDT (timeout=wd_timeout_ms)

    def poll_it (self) :
        #print ("Watchdog: poll_it")
        self.wdt.feed ()           # Still going

    def shutdown (self) :
        pass

# end Watchdog #

#---------------------------------------------------------------------------
# main
#---------------------------------------------------------------------------

if MACHINE_FREQ > 0 :
    machine.freq (MACHINE_FREQ)
    print ("machine.freq:", machine.freq())
#----
#---- Display set up
#----
spi = machine.SPI(1,
                  baudrate=30000000 ,
                  polarity=1 ,
                  sck=machine.Pin(18) ,
                  mosi=machine.Pin(19))
display = st7789.ST7789(spi ,
                        HEIGHT ,
                        WIDTH ,
                        reset=machine.Pin(23, machine.Pin.OUT) ,
                        cs=machine.Pin(5, machine.Pin.OUT) ,
                        dc=machine.Pin(16, machine.Pin.OUT) ,
                        backlight=Pin(4, Pin.OUT) ,
                        rotation=ROTATION
                        )
display.init()
display.fill (BG_COLOR)

#----
#---- Polling controller set up
#----
poller = PollLooper (poll_ms = 100)             # 0.10 second poll

#----
#---- User PlugIn's
#----
#get_command = GetCommand (poller)              # Get extrnal updates
#power_control = PowerControl (poller)          # Sets power level

#----
#---- Add plugins to poll array - determines which plugin's are polled and polling order
#----
poller.poll_add (Watchdog ())
poller.poll_add (ShutdownTimer (poller,        # Shut down after time setting
                                hours = SHUTDOWN_HOURS ,
                                minutes = SHUTDOWN_MINUTES ,
                                seconds = SHUTDOWN_SECONDS))
poller.poll_add (PollIndicator (poller ,       # Indicates polling
                                xpos = 0 ,
                                ypos = 0 ,
                                color = GREEN))
poller.poll_add (GetCommand (poller))
poller.poll_add (PowerControl (poller))

#----
#---- Start polling
#----
poller.poll_start ()

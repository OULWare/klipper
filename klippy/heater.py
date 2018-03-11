# Printer heater support
#
# Copyright (C) 2016,2017  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, threading
import pins, reactor
import extras.sensors as sensors


######################################################################
# Heater
######################################################################

SAMPLE_TIME = 0.001
SAMPLE_COUNT = 8
REPORT_TIME = 0.300
MAX_HEAT_TIME = 5.0
AMBIENT_TEMP = 25.
PID_PARAM_BASE = 255.

class error(Exception):
    pass

class PrinterHeater:
    error = error
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.gcode = printer.lookup_object('gcode')
        self.name = config.get_name()
        try:
            self.index = int(self.name[7:])
        except ValueError:
            self.index = -1 # Mark to bed
        self.logger = printer.logger.getChild(self.name.replace(" ", "_"))
        sensor_name = config.get('sensor')
        self.logger.debug("Add heater '{}', index {}, sensor {}".
                          format(self.name, self.index, sensor_name))
        self.sensor = sensors.load_sensor(
            config.getsection('sensor %s'%sensor_name))
        self.min_temp, self.max_temp = self.sensor.get_min_max_temp()
        self.min_extrude_temp = 170. # Set by the extruder
        self.min_extrude_temp_disabled = False
        self.max_power = config.getfloat('max_power', 1., above=0., maxval=1.)
        self.lock = threading.Lock()
        self.last_temp = 0.
        self.last_temp_time = 0.
        self.target_temp = 0.
        algos = {'watermark': ControlBangBang, 'pid': ControlPID}
        algo = config.getchoice('control', algos)
        heater_pin = config.get('heater_pin')
        if algo is ControlBangBang and self.max_power == 1.:
            self.mcu_pwm = pins.setup_pin(printer, 'digital_out', heater_pin)
        else:
            self.mcu_pwm = pins.setup_pin(printer, 'pwm', heater_pin)
            pwm_cycle_time = config.getfloat(
                'pwm_cycle_time', 0.100, above=0., maxval=REPORT_TIME)
            self.mcu_pwm.setup_cycle_time(pwm_cycle_time)
        self.mcu_pwm.setup_max_duration(MAX_HEAT_TIME)
        self.mcu_sensor = self.sensor.get_mcu()
        self.mcu_sensor.setup_adc_callback(REPORT_TIME, self.adc_callback)
        is_fileoutput = self.mcu_sensor.get_mcu().is_fileoutput()
        self.can_extrude = self.min_extrude_temp <= 0. or is_fileoutput
        self.control = algo(self, config)
        # pwm caching
        self.next_pwm_time = 0.
        self.last_pwm_value = 0.
        # heat check timer
        self.protection_period_heat = \
            config.getfloat('protect_period_heat', 10.0, above=0.0, maxval=120.0)
        self.protection_hysteresis_heat = \
            config.getfloat('protect_hysteresis_heat', 4.0, above=0.50)
        self.protection_period = \
            config.getfloat('protect_period', 10.0, above=0.0, maxval=120.0)
        self.protect_hyst_runaway = \
            config.getfloat('protect_hysteresis_runaway', 4.0, above=0.0)
        self.reactor = printer.reactor
        self.protection_timer = self.reactor.register_timer(self._check_heating)

    def _check_heating(self, eventtime):
        next_time = 10.0 # next 10sec from now

        with self.lock:
            current_temp = self.last_temp
            target_temp = self.target_temp

        if (self.protection_last_temp is None):
            self.is_heating = False
            self.is_runaway = False
            self.is_cooling = False
            # Set init value
            self.protection_last_temp = current_temp

            if (current_temp <= (target_temp - self.protect_hyst_runaway)):
                self.is_heating = True
                next_time = self.protection_period_heat
            elif (target_temp < current_temp and 0 < target_temp):
                self.is_cooling = True
                next_time = self.protection_period
            else:
                self.is_runaway = True
                next_time = self.protection_period
        elif self.is_runaway:
            # Check hysteresis during maintain
            if (self.protect_hyst_runaway < abs(current_temp - target_temp)):
                errorstr = "Thermal runaway! current temp {}, last {}". \
                           format(current_temp, self.protection_last_temp)
                self.set_temp(0, 0);
                self.gcode.respond_stop(errorstr)
                #self.printer.request_exit('firmware_restart')
                self.printer.request_exit('shutdown')
            self.protection_last_temp = current_temp
            next_time = self.protection_period
        elif (self.is_cooling):
            next_time = self.protection_period_heat
            if ((current_temp - self.protect_hyst_runaway) < target_temp):
                self.is_cooling = False
                self.is_heating = True
        elif (self.is_heating):
            # Check hysteresis during the preheating
            if ((target_temp - self.protect_hyst_runaway) \
                <= current_temp <=
                (target_temp + self.protect_hyst_runaway)):
                self.is_runaway = True;
            elif (current_temp < target_temp):
                if (abs(current_temp - self.protection_last_temp) < self.protection_hysteresis_heat):
                    errorstr = "Heating error! current temp {}, last {}". \
                               format(current_temp, self.protection_last_temp)
                    self.set_temp(0, 0);
                    self.gcode.respond_stop(errorstr)
                    #self.printer.request_exit('firmware_restart')
                    self.printer.request_exit('shutdown')
            self.protection_last_temp = current_temp
            next_time = self.protection_period_heat
        self.logger.debug("check_heating(eventtime {}, next {}) {} / {}".
                          format(eventtime, (eventtime + next_time),
                                 current_temp, target_temp))
        return eventtime + next_time
    def get_min_extrude_status(self):
        stat = "prevented"
        if self.min_extrude_temp_disabled:
            stat = "allowed"
        return stat, self.min_extrude_temp
    def set_min_extrude_temp(self, temp, disable=None):
        if disable is not None:
            self.min_extrude_temp_disabled = disable
        if temp is None:
            return
        if (self.max_temp < temp):
            raise error("min_extrude_temp {} is not between min_temp {} and max_temp {}!"
                        .format(temp, self.min_temp, self.max_temp))
        self.min_extrude_temp = temp;
        is_fileoutput = self.mcu_sensor.get_mcu().is_fileoutput()
        self.can_extrude = (self.min_extrude_temp <= self.min_temp) or \
                           self.min_extrude_temp_disabled or \
                           is_fileoutput
    def set_pwm(self, read_time, value):
        if self.target_temp <= 0.:
            value = 0.
        if ((read_time < self.next_pwm_time or not self.last_pwm_value)
            and abs(value - self.last_pwm_value) < 0.05):
            # No significant change in value - can suppress update
            return
        pwm_time = read_time + REPORT_TIME + self.sensor.sample_time*self.sensor.sample_count
        self.next_pwm_time = pwm_time + 0.75 * MAX_HEAT_TIME
        self.last_pwm_value = value
        self.logger.debug("%s: pwm=%.3f@%.3f (from %.3f@%.3f [%.3f])",
                          self.name, value, pwm_time,
                          self.last_temp, self.last_temp_time, self.target_temp)
        self.mcu_pwm.set_pwm(pwm_time, value)
    def adc_callback(self, read_time, read_value, fault = 0):
        if (fault):
            self.sensor.check_faults(fault)
        temp = self.sensor.calc_temp(read_value)
        with self.lock:
            self.last_temp = temp
            self.last_temp_time = read_time
            self.can_extrude = self.min_extrude_temp_disabled or \
                               (temp >= self.min_extrude_temp)
            self.control.adc_callback(read_time, temp)
        #self.logger.debug("read_time=%.3f read_value=%f temperature=%f",
        #                  read_time, read_value, temp)
    # External commands
    def set_temp(self, print_time, degrees, auto_tune=False):
        if degrees and (degrees < self.min_temp or degrees > self.max_temp):
            raise error("Requested temperature (%.1f) out of range (%.1f:%.1f)"
                        % (degrees, self.min_temp, self.max_temp))
        with self.lock:
            self.target_temp = degrees
        if (degrees and auto_tune is False):
            # Start checking
            self.protection_last_temp = None
            self.reactor.update_timer(self.protection_timer,
                                      self.reactor.NOW)
            self.logger.debug("Temperature protection timer started")
        else:
            # stop checking
            self.reactor.update_timer(self.protection_timer,
                                      self.reactor.NEVER)
            self.logger.debug("Temperature protection timer stopped")

    def get_temp(self, eventtime):
        print_time = self.mcu_sensor.get_mcu().estimated_print_time(eventtime) - 5.
        with self.lock:
            if self.last_temp_time < print_time:
                return 0., self.target_temp
            return self.last_temp, self.target_temp
    def check_busy(self, eventtime):
        with self.lock:
            return self.control.check_busy(eventtime)
    def start_auto_tune(self, degrees):
        #if degrees and (degrees < self.min_temp or degrees > self.max_temp):
        #    raise error("Requested temperature (%.1f) out of range (%.1f:%.1f)"
        #                % (degrees, self.min_temp, self.max_temp))
        with self.lock:
            self.control = ControlAutoTune(self, self.control)
        self.set_temp(0, degrees, auto_tune=True)
    def finish_auto_tune(self, old_control):
        if (type(self.control).__name__ is "ControlAutoTune" and \
            type(old_control).__name__ is ControlPID):
            kp, ki, kd = self.control.get_terms();
            old_control.set_new_terms(kp, ki, kd)
        self.control = old_control
        self.set_temp(0, 0)
    def stats(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            last_temp = self.last_temp
            last_pwm_value = self.last_pwm_value
        is_active = target_temp or last_temp > 50.
        return is_active, '%s: target=%.0f temp=%.1f pwm=%.3f' % (
            self.name, target_temp, last_temp, last_pwm_value)
    def get_status(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            last_temp = self.last_temp
        return {'temperature': last_temp, 'target': target_temp}


######################################################################
# Bang-bang control algo
######################################################################

class ControlBangBang:
    def __init__(self, heater, config):
        self.logger = heater.logger.getChild('bangbang')
        self.heater = heater
        self.max_delta = config.getfloat('max_delta', 2.0, above=0.)
        self.heating = False
    def adc_callback(self, read_time, temp):
        if self.heating and temp >= self.heater.target_temp+self.max_delta:
            self.heating = False
        elif not self.heating and temp <= self.heater.target_temp-self.max_delta:
            self.heating = True
        if self.heating:
            self.heater.set_pwm(read_time, self.heater.max_power)
        else:
            self.heater.set_pwm(read_time, 0.)
    def check_busy(self, eventtime):
        return (self.heater.last_temp < (self.heater.target_temp-self.max_delta)) or \
            ((self.heater.target_temp+self.max_delta) < self.heater.last_temp)


######################################################################
# Proportional Integral Derivative (PID) control algo
######################################################################

PID_SETTLE_DELTA = 1.
PID_SETTLE_SLOPE = .1

class ControlPID:
    def __init__(self, heater, config):
        self.logger = heater.logger.getChild('pid')
        self.heater = heater
        self.Kp = config.getfloat('pid_Kp') / PID_PARAM_BASE
        self.Ki = config.getfloat('pid_Ki') / PID_PARAM_BASE
        self.Kd = config.getfloat('pid_Kd') / PID_PARAM_BASE
        self.min_deriv_time = config.getfloat('pid_deriv_time', 2., above=0.)
        self.imax = config.getfloat('pid_integral_max', heater.max_power, minval=0.)
        self.temp_integ_max = self.imax / self.Ki
        self.prev_temp = AMBIENT_TEMP
        self.prev_temp_time = 0.
        self.prev_temp_deriv = 0.
        self.prev_temp_integ = 0.
    def set_new_terms(self, Kp, Ki, Kd):
        self.Kp = Kp
        self.Ki = Ki
        self.Kd = Kd
        # reset adjustment
        self.temp_integ_max = self.imax / self.Ki
        self.prev_temp = AMBIENT_TEMP
        self.prev_temp_time = 0.
        self.prev_temp_deriv = 0.
        self.prev_temp_integ = 0.
    def adc_callback(self, read_time, temp):
        time_diff = read_time - self.prev_temp_time
        # Calculate change of temperature
        temp_diff = temp - self.prev_temp
        if time_diff >= self.min_deriv_time:
            temp_deriv = temp_diff / time_diff
        else:
            temp_deriv = (self.prev_temp_deriv * (self.min_deriv_time-time_diff)
                          + temp_diff) / self.min_deriv_time
        # Calculate accumulated temperature "error"
        temp_err = self.heater.target_temp - temp
        temp_integ = self.prev_temp_integ + temp_err * time_diff
        temp_integ = max(0., min(self.temp_integ_max, temp_integ))
        # Calculate output
        co = self.Kp*temp_err + self.Ki*temp_integ - self.Kd*temp_deriv
        #self.logger.debug("pid: %f@%.3f -> diff=%f deriv=%f err=%f integ=%f co=%d",
        #    temp, read_time, temp_diff, temp_deriv, temp_err, temp_integ, co)
        bounded_co = max(0., min(self.heater.max_power, co))
        self.heater.set_pwm(read_time, bounded_co)
        # Store state for next measurement
        self.prev_temp = temp
        self.prev_temp_time = read_time
        self.prev_temp_deriv = temp_deriv
        if co == bounded_co:
            self.prev_temp_integ = temp_integ
    def check_busy(self, eventtime):
        temp_diff = self.heater.target_temp - self.heater.last_temp
        return (abs(temp_diff) > PID_SETTLE_DELTA
                or abs(self.prev_temp_deriv) > PID_SETTLE_SLOPE)


######################################################################
# Ziegler-Nichols PID autotuning
######################################################################

TUNE_PID_DELTA = 5.0

class ControlAutoTune:
    Kp = None
    Ki = None
    Kd = None
    def __init__(self, heater, old_control):
        self.logger = heater.logger.getChild('autotune')
        self.heater = heater
        self.old_control = old_control
        self.heating = False
        self.peaks = []
        self.peak = 0.
        self.peak_time = 0.
    def adc_callback(self, read_time, temp):
        if self.heating and temp >= self.heater.target_temp:
            self.heating = False
            self.check_peaks()
        elif (not self.heating
              and temp <= self.heater.target_temp - TUNE_PID_DELTA):
            self.heating = True
            self.check_peaks()
        if self.heating:
            self.heater.set_pwm(read_time, self.heater.max_power)
            if temp < self.peak:
                self.peak = temp
                self.peak_time = read_time
        else:
            self.heater.set_pwm(read_time, 0.)
            if temp > self.peak:
                self.peak = temp
                self.peak_time = read_time
    def check_peaks(self):
        self.peaks.append((self.peak, self.peak_time))
        if self.heating:
            self.peak = 9999999.
        else:
            self.peak = -9999999.
        if len(self.peaks) < 4:
            return
        self.calc_pid(len(self.peaks)-1)
    def calc_pid(self, pos):
        temp_diff = self.peaks[pos][0] - self.peaks[pos-1][0]
        time_diff = self.peaks[pos][1] - self.peaks[pos-2][1]
        max_power = self.heater.max_power
        Ku = 4. * (2. * max_power) / (abs(temp_diff) * math.pi)
        Tu = time_diff

        Ti = 0.5 * Tu
        Td = 0.125 * Tu
        Kp = 0.6 * Ku * PID_PARAM_BASE
        Ki = Kp / Ti
        Kd = Kp * Td
        self.logger.info("Autotune: raw=%f/%f Ku=%f Tu=%f  Kp=%f Ki=%f Kd=%f",
                     temp_diff, max_power, Ku, Tu, Kp, Ki, Kd)
        return Kp, Ki, Kd
    def final_calc(self):
        cycle_times = [(self.peaks[pos][1] - self.peaks[pos-2][1], pos)
                       for pos in range(4, len(self.peaks))]
        midpoint_pos = sorted(cycle_times)[len(cycle_times)/2][1]
        self.Kp, self.Ki, self.Kd = self.calc_pid(midpoint_pos)
        logging.info("Autotune: final: Kp=%f Ki=%f Kd=%f", self.Kp, self.Ki, self.Kd)
        gcode = self.heater.printer.lookup_object('gcode')
        gcode.respond_info(
            "PID parameters: pid_Kp=%.3f pid_Ki=%.3f pid_Kd=%.3f\n"
            "To use these parameters, update the printer config file with\n"
            "the above and then issue a RESTART command" % (self.Kp, self.Ki, self.Kd))
    def check_busy(self, eventtime):
        if self.heating or len(self.peaks) < 12:
            return True
        self.final_calc()
        self.heater.finish_auto_tune(self.old_control)
        return False
    def get_terms(self):
        return self.Kp, self.Ki, self.Kd

######################################################################
# Tuning information test
######################################################################

class ControlBumpTest:
    def __init__(self, heater, old_control):
        self.heater = heater
        self.old_control = old_control
        self.temp_samples = {}
        self.pwm_samples = {}
        self.state = 0
    def set_pwm(self, read_time, value):
        self.pwm_samples[read_time + 2*REPORT_TIME] = value
        self.heater.set_pwm(read_time, value)
    def adc_callback(self, read_time, temp):
        self.temp_samples[read_time] = temp
        if not self.state:
            self.set_pwm(read_time, 0.)
            if len(self.temp_samples) >= 20:
                self.state += 1
        elif self.state == 1:
            if temp < self.heater.target_temp:
                self.set_pwm(read_time, self.heater.max_power)
                return
            self.set_pwm(read_time, 0.)
            self.state += 1
        elif self.state == 2:
            self.set_pwm(read_time, 0.)
            if temp <= (self.heater.target_temp + AMBIENT_TEMP) / 2.:
                self.dump_stats()
                self.state += 1
    def dump_stats(self):
        out = ["%.3f %.1f %d" % (time, temp, self.pwm_samples.get(time, -1.))
               for time, temp in sorted(self.temp_samples.items())]
        f = open("/tmp/heattest.txt", "wb")
        f.write('\n'.join(out))
        f.close()
    def check_busy(self, eventtime):
        if self.state < 3:
            return True
        self.heater.finish_auto_tune(self.old_control)
        return False

def load_config(config):
    raise config.get_printer().config_error(
        "Naming without index (bed or [0-9]+) is not allowed")

def load_config_prefix(config):
    return PrinterHeater(config)

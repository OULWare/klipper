import fan

PIN_MIN_TIME = 0.100

class MotorFan:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.fan = fan.PrinterFan(config)
        self.mcu = self.fan.mcu_fan.get_mcu()
        max_power = self.fan.max_power
        self.fan_speed = config.getfloat(
            "fan_speed", max_power, minval=0., maxval=max_power)
        self.fan.mcu_fan.setup_start_value(0., max_power)
    def printer_state(self, state):
        if state == 'ready':
            self.toolhead = self.printer.lookup_object('toolhead')
            self.logger = self.fan.logger = \
                          self.toolhead.logger.getChild(
                              self.fan.name.replace(" ", "_"))
            self.toolhead.register_cb('motor', self.callback)
            self.reactor = self.printer.get_reactor()
    def callback(self, event, eventtime):
        if event == 'off':
            self.power = 0.
        else:
            self.power = self.fan_speed
        self.set_timer = self.reactor.register_timer(self.reactor_callback,
                                                     self.reactor.NOW)
    def reactor_callback(self, eventtime):
        print_time = self.mcu.estimated_print_time(eventtime) + PIN_MIN_TIME
        self.fan.set_speed(print_time, self.power)
        return self.reactor.NEVER

def load_config(config):
    return MotorFan(config)
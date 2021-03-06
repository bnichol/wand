import logging
import time
from wand.tools import WLMMeasurementStatus

try:  # allow us to run simulations on Linux
    from ctypes import windll, c_double, c_ushort, c_long, c_bool, byref
except ImportError:
    pass

from wand.drivers import wlm_constants as wlm

logger = logging.getLogger(__name__)


class WLMException(Exception):
    """ Raised on errors involving the WLM interface library (windata.dll) """
    pass


class WLM:
    """" Driver for HighFinesse WaveLength Meters (WLM) """
    def __init__(self, simulation):
        self._num_ccds = 2

        self.simulation = simulation
        if self.simulation:
            self._exp_min = [2]*2
            self._exp_max = [999]*2
            self._exposure = [self._exp_min] * 2
            return

        self.active_switch_ch = 1

        try:
            self.lib = lib = windll.wlmData
        except Exception as e:
            raise WLMException("Failed to load WLM DLL: {}".format(e))

        # configure DLL function arg/return types
        lib.Operation.restype = c_long
        lib.Operation.argtypes = [c_ushort]
        lib.GetOperationState.restype = c_ushort
        lib.GetOperationState.argtypes = [c_ushort]
        lib.GetTemperature.restype = c_double
        lib.GetTemperature.argtypes = [c_double]
        lib.GetPressure.restype = c_double
        lib.GetPressure.argtypes = [c_double]
        lib.SetExposureModeNum.restype = c_long
        lib.SetExposureModeNum.argtypes = [c_long, c_bool]
        lib.GetFrequencyNum.restype = c_double
        lib.GetFrequencyNum.argtypes = [c_long, c_double]

        # Check the WLM server application is running and start it if necessary
        if not lib.Instantiate(wlm.cInstCheckForWLM, 0, 0, 0):
            logger.info("Starting WLM server")
            res = lib.ControlWLMEx(wlm.cCtrlWLMShow | wlm.cCtrlWLMWait,
                                   0, 0, 10000, 1)
            codes = wlm.control_wlm_to_str(res)
            if "flServerStarted" not in codes:
                raise WLMException("Error starting WLM server application : "
                                   "{}".format(codes))
            for code in codes:
                if code == "flServerStarted":
                    continue
                logger.warning("Unexpected return code from ControlWLMEx: {} "
                               .format(code))

        logger.info("Connected to WLM server")

        self.wlm_model = lib.GetWLMVersion(0)
        self.wlm_hw_rev = lib.GetWLMVersion(1)
        self.wlm_fw_rev = lib.GetWLMVersion(2)
        self.wlm_fw_build = lib.GetWLMVersion(3)

        if self.wlm_model < 5 or self.wlm_model > 8:
            raise WLMException("Unrecognised WLM model: {}".format(
                self.wlm_ve))

        if lib.GetOperationState(0) == wlm.cStop:
            self.set_measurement_enabled(True)

        if lib.SetSwitcherMode(0) < 0:  # disable automatic channel switching
            logger.warning("Unable to disable automatic WLM switching")

        # fixme: hard-code that we have two ccds for now
        # fix me: setting exp 2 to exp_min gives errors. Works fine via the GUI
        self._exp_min = [lib.GetExposureRange(wlm.cExpoMin),
                         lib.GetExposureRange(wlm.cExpo2Min)
                         ]
        self._exp_max = [lib.GetExposureRange(wlm.cExpoMax),
                         lib.GetExposureRange(wlm.cExpo2Max)
                         ]

        # fixme: hack to work around bug in WLM software
        if self._exp_min[1] == 0:
            self._exp_min[1] = 2

        self._exposure = self._exp_min.copy()
        self._set_exp = [[lib.GetExposureNum(ch+1, 1),
                          lib.GetExposureNum(ch+1, 2)] for ch in range(8)]

        if 0 in self._exp_min or 0 in self._exp_max:
            raise WLMException("Error finding WLM exposure range")

        for channel in range(8):  # manual exposure
            if lib.SetExposureModeNum(channel + 1, 0) < 0:
                logger.warning("Error setting WLM exposure mode")

            # hook up wait for event mechanism
        if self.lib.Instantiate(wlm.cInstNotification,
                                wlm.cNotifyInstallWaitEvent,
                                c_long(max(self._exp_max)+100),  # timeout
                                0) == 0:
            raise WLMException("Error hooking up WLM callbacks")

        # set to manual measurement control
        self._update_exposure(self._exp_min, block=False)
        self._trigger_single_measurement()

        logger.info("Connected to " + self.identify())

    def identify(self):
        """ :returns: WLM identification string """
        if self.simulation:
            return "WLM simulator"

        return "WLM {} rev {}, firmware {}.{}".format(
            self.wlm_model, self.wlm_hw_rev, self.wlm_fw_rev,
            self.wlm_fw_build)

    def set_measurement_enabled(self, enabled):
        """ :param enabled: if True, we enable WLM measurement mode """
        if self.simulation:
            return

        mode = wlm.cCtrlStartMeasurement if enabled else wlm.cCtrlStopAll
        if self.lib.Operation(mode) < 0:
            raise WLMException("Error starting WLM measuremnts")

    def get_temperature(self):
        """ Returns the temperature of the wavemeter in C """
        if self.simulation:
            return 25.0

        temp = self.lib.GetTemperature(0)
        if temp < 0:
            raise WLMException(
                "Error reading WLM temperature: {}".format(temp))
        return temp

    def get_pressure(self):
        """ Returns the pressure inside the wavemeter in mBar
        :raises WLMException: with an error code of -1006 if the wavemeter does
          not support pressure measurements
        """
        if self.simulation:
            return 1013.25

        pressure = self.lib.GetPressure(0)
        if pressure < 0:
            raise WLMException(
                "Error reading WLM pressure: {}". format(pressure))
        return pressure

    def _update_exposure(self, exposure=None, block=True):
        """ Updates the WLM exposure times:

        :param exposure: if None then we use self._exposure, otherwise this
        should be a list of exposure times to set.
        :param block: if True, this method blocks until the exposure update is
        complete.
        """
        max_ccd_changed = -1
        exposure = self._exposure if exposure is None else exposure
        for ccd, exp in enumerate(exposure):
            if exp == self._set_exp[self.active_switch_ch-1][ccd]:
                continue
            self._set_exp[self.active_switch_ch-1][ccd] = exp

            if 0 > self.lib.SetExposureNum(self.active_switch_ch, ccd+1, exp):
                raise WLMException("Unable to set WLM exposure time for ccd {}"
                                   " to {} ms".format(ccd, exp))
            max_ccd_changed = ccd

        if max_ccd_changed == -1:
            return

        event = vars(wlm)["cmiExposureValue{}{}".format(max_ccd_changed+1,
                                                        self.active_switch_ch)]
        if block:
            self._wait_for_event([event], exposure[max_ccd_changed])

    def _get_fresh_data(self):
        """ Gets a "fresh" wavelength measurement, guaranteed to occur after
        this method was called.

        The WLM has an (undocumented) internal measurement pipeline three
        events deep. As a result, to get fresh data we need to perform three
        measurements. To minimize the time wasted flushing the measurement
        pipeline, we set the exposure time to minimum during the "dummy"
        measurements.
        """
        self._update_exposure()  # synchronise WLM with self._exposure
        for pipeline_stage in range(3):
            self._trigger_single_measurement()
            if pipeline_stage == 1:
                self._update_exposure(self._exp_min)

    def _trigger_single_measurement(self):
        """
        Triggers a new WLM measurement cycle and blocks until it completes.
        """
        self.lib.ClearWLMEvents()  # flush the WLM pipeline

        if self.lib.TriggerMeasurement(wlm.cCtrlMeasurementTriggerSuccess):
            raise WLMException("Error triggering WLM measurement cycle")

        self._wait_for_event([wlm.cmiTriggerState],
                             wlm.cCtrlMeasurementTriggerSuccess)

    def _wait_for_event(self, event_values, int_value):
        """ Wait for a WLM event to occur """
        event = c_long()
        p_int = c_long()
        p_double = c_double()
        ret = 0

        logger.debug("Waiting for WLM event...")

        t0 = time.time()
        while True:
            ret = self.lib.WaitForWLMEvent(byref(event),
                                           byref(p_int),
                                           byref(p_double))
            if ret == -1:
                raise WLMException("Timeout while waiting for event")
            elif ret == 1:
                ret_str = "more in pipeline"
            elif ret == 2:
                ret_str = "pipeline empty"
            else:
                raise WLMException("Unexpected return from WaitForWLMEvent"
                                   ": {}".format(ret))

            logger.debug("{} ms: {}, current state='{}' ({}, {})".format(
                1e3*(time.time() - t0),
                ret_str,
                wlm.event_to_str(event),
                p_int.value,
                p_double.value))

            # NB: cmiTriggerState appears twice:
            #   - once directly after the TriggerMeasurement call, with
            #     p_int = cCtrlMeasurementContinue
            #   - and once at the end of the measurement, with
            #     p_int = cCtrlMeasurementTriggerSuccess
            if event.value in event_values and p_int.value == int_value:
                break
        logger.debug("...done")

    def get_frequency(self):
        """ Returns the frequency of the active channel.

        The frequency measurement is guaranteed to occur after this method is
        called. See :meth _get_fresh_data: for details.

        :returns: the tuple (status, frequency) where status is a
          WLMMeasurementStatus and frequency is in Hz.
        """
        if self.simulation:
            return 0, WLMMeasurementStatus.OKAY

        # this should never time out, but it does...
        # I've had a long discussion with the HF engineers about why this
        # occurs on some units, without any real success.
        try:
            self._get_fresh_data()
            freq = self.lib.GetFrequencyNum(self.active_switch_ch, 0)
        except WLMException as e:
            logger.error("error during frequency read: {}".format(e))
            return WLMMeasurementStatus.ERROR, 0

        if freq > 0:
            return WLMMeasurementStatus.OKAY, freq*1e12
        elif freq == wlm.ErrBigSignal:
            return WLMMeasurementStatus.OVER_EXPOSED, 0
        elif freq == wlm.ErrLowSignal:
            return WLMMeasurementStatus.UNDER_EXPOSED, 0
        else:
            logger.error("error getting frequency: {}"
                         .format(wlm.error_to_str(freq)))
            return WLMMeasurementStatus.ERROR, 0

    def get_exposure_min(self):
        """ Returns the minimum exposure times in ms """
        return self._exp_min

    def get_exposure_max(self):
        """ Returns the maximum exposure times in ms """
        return self._exp_max

    def get_num_ccds(self):
        """ Returns the number of CCDs on the WLM """
        return self._num_ccds

    def set_exposure(self, exposure, ccd):
        """ Sets the exposure time for the active channel

        :param exposure: exposure time (ms)
        :param ccd: the WLM ccd to set the exposure for. Must lie in
          range(self._num_ccds)
        """
        exposure = int(exposure)
        if exposure < self._exp_min[ccd] or exposure > self._exp_max[ccd]:
            raise WLMException("Invalid WLM exposure for ccd {}: {} ms"
                               .format(ccd, exposure))
        if ccd not in range(self._num_ccds):
            raise WLMException("Invalid ccd: {}".format(ccd))
        self._exposure[ccd] = exposure

    def get_fringe_peak(self, ccd):
        """ Returns the peak height of the interference pattern normalized
        to full scale. Used for auto exposure etc """
        if ccd not in range(self._num_ccds):
            raise WLMException("Invalid ccd: {}".format(ccd))
        if self.simulation:
            return 0.5

        peak = self.lib.GetAmplitudeNum(self.active_switch_ch,
                                        [wlm.cMax1, wlm.cMax2][ccd], 0)
        if peak < 0:
            logger.error("error getting peak height: {}"
                         .format(wlm.error_to_str(peak)))
            return WLMMeasurementStatus.ERROR, 0
        return peak/3500.  # to do, figure out what the scale factor should be!

    def get_switch(self):
        """ :returns: an interface to the WLM integrated switch """
        return self.Switch(self)

    class Switch:
        """ High-Finesse fibre switch controlled by the WLM """
        def __init__(self, wlm):
            self._wlm = wlm

        def get_num_channels(self):
            """ Returns the number of channels on the switch """
            return 8

        def set_active_channel(self, channel):
            """ Sets the active channel.

            :param channel: the channel number to select, not zero-indexed
            """
            if channel < 1 or channel > 8:
                raise WLMException("Invalid WLM switch channel number")

            self._wlm.active_switch_ch = channel

            if self._wlm.simulation:
                return
            ret = self._wlm.lib.SetSwitcherChannel(channel)
            if ret < 0:
                raise WLMException(
                    "Unable to set WLM switcher channel to: {}".format(channel,
                                                                       ret))
            try:
                self._wlm._wait_for_event([wlm.cmiSwitcherChannel], channel)
            except WLMException as e:
                logger.error("Error changing switcher channel: {}".format(e))

        def get_active_channel(self):
            """ Returns the active channel number
            :return: the active channel, not zero-indexed
            """
            if self._wlm.simulation:
                return 1
            channel = self._wlm.lib.GetSwitcherChannel(0)
            if channel < 0 or channel > 8:
                raise WLMException("Unable to query active WLM switcher "
                                   "channel")
            return channel

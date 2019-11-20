import logging
import threading

from pyobs.interfaces import ICamera, IMotion, ICameraBinning, ICameraWindow
from pyobs.utils.threads import Future
from pyobs.utils.threads.checkabort import check_abort
from .base import LcoBaseConfig


log = logging.getLogger(__name__)


class LcoDefaultConfig(LcoBaseConfig):
    def __init__(self, *args, **kwargs):
        LcoBaseConfig.__init__(self, *args, **kwargs)

    def can_run(self) -> bool:
        """Whether this config can currently run."""

        # we need an open roof and a working telescope
        if self.roof.get_motion_status().wait() not in [IMotion.Status.POSITIONED, IMotion.Status.TRACKING] or \
                self.telescope.get_motion_status().wait() != IMotion.Status.IDLE:
            return False

        # seems alright
        return True

    def __call__(self, abort_event: threading.Event) -> int:
        """Run configuration.

        Args:
            abort_event: Event to abort run.

        Returns:
            Total exposure time in ms.
        """

        # got a target?
        target = self.config['target']
        track = None
        if target['ra'] is not None and target['dec'] is not None:
            log.info('Moving to target %s...', target['name'])
            track = self.telescope.track_radec(target['ra'], target['dec'])

        # total exposure time in this config
        exp_time_done = 0

        # loop instrument configs
        for ic in self.config['instrument_configs']:
            check_abort(abort_event)

            # set filter
            set_filter = None
            if 'optical_elements' in ic and 'filter' in ic['optical_elements']:
                log.info('Setting filter to %s...', ic['optical_elements']['filter'])
                set_filter = self.filters.set_filter(ic['optical_elements']['filter'])

            # wait for tracking and filter
            Future.wait_all([track, set_filter])

            # set binning and window
            check_abort(abort_event)
            if isinstance(self.camera, ICameraBinning):
                log.info('Set binning to %dx%d...', ic['bin_x'], ic['bin_x'])
                self.camera.set_binning(ic['bin_x'], ic['bin_x']).wait()
            if isinstance(self.camera, ICameraWindow):
                full_frame = self.camera.get_full_frame().wait()
                self.camera.set_window(*full_frame).wait()

            # loop images
            for exp in range(ic['exposure_count']):
                check_abort(abort_event)
                log.info('Exposing %s image %d/%d for %.2fs...',
                         self.config['type'], exp + 1, ic['exposure_count'], ic['exposure_time'])
                self.camera.expose(int(ic['exposure_time'] * 1000), self.image_type).wait()
                exp_time_done += ic['exposure_time']

        # finally, return total exposure time
        return exp_time_done


__all__ = ['LcoDefaultConfig']
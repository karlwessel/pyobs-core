import logging
import threading
from enum import Enum
from typing import Union
import pandas as pd

from pyobs import PyObsModule
from pyobs.interfaces import ICamera, ISettings
from pyobs.modules import timeout
from pyobs.events import NewImageEvent, ExposureStatusChangedEvent
from pyobs.utils.images import Image
from pyobs.utils.photometry import SepPhotometry

log = logging.getLogger(__name__)


class AdaptiveCameraMode(Enum):
    # find brightest star within radius around centre of image
    CENTRE = 'centre',
    # find brightest star in whole image
    BRIGHTEST = 'brightest'


class AdaptiveCamera(PyObsModule, ICamera, ISettings):
    """A virtual camera for adaptive exposure times."""

    def __init__(self, camera: str, mode: Union[str, AdaptiveCameraMode] = AdaptiveCameraMode.CENTRE, radius: int = 20,
                 target_counts: int = 30000, min_exptime: int = 500, max_exptime: int = 60000,
                 *args, **kwargs):
        """Creates a new adaptive exposure time camera.

        Args:
            camera: Actual camera to use.
            mode: Which mode to use to find star.
            radius: Radius in px around centre for CENTRE mode.
            target_counts: Counts to aim for in target.
            min_exptime: Minimum exposure time.
            max_exptime: Maximum exposure time.
        """
        PyObsModule.__init__(self, *args, **kwargs)

        # store
        self._camera_name = camera
        self._camera = None
        self._mode = mode if isinstance(mode, AdaptiveCameraMode) else AdaptiveCameraMode(mode)
        self._radius = radius

        # abort
        self._abort = threading.Event()

        # exposures
        self._exp_time = None
        self._exposure_count = None
        self._exposures_done = None

        # options
        self._counts = target_counts
        self._min_exp_time = min_exptime
        self._max_exp_time = max_exptime

        # SEP
        self._sep = SepPhotometry()

        # add thread
        self._process_filename = None
        self._process_lock = threading.RLock()
        self._add_thread_func(self._process_thread, True)

    def open(self):
        """Open module."""
        PyObsModule.open(self)

        # subscribe to events
        if self.comm:
            self.comm.register_event(NewImageEvent)
            self.comm.register_event(ExposureStatusChangedEvent, self._status_changed)

        # get link to camera
        self._camera = self.proxy(self._camera_name, ICamera)

    @timeout('(exposure_time+10000)*count')
    def expose(self, exposure_time: int, image_type: ICamera.ImageType, count: int = 1, broadcast: bool = True,
               *args, **kwargs) -> list:
        """Starts exposure and returns reference to image.

        Args:
            exposure_time: Exposure time in seconds.
            image_type: Type of image.
            count: Number of images to take.
            broadcast: Broadcast existence of image.

        Returns:
            List of references to the image that was taken.
        """

        # reset
        self._abort = threading.Event()
        self._exp_time = exposure_time
        self._exposure_count = count
        self._exposures_done = 0

        # loop exposures
        return_filenames = []
        for i in range(count):
            # abort?
            if self._abort.is_set():
                break

            # do exposure(s), never broadcast
            log.info('Starting exposure with %d/%d for %.2fs...', i+1, count, self._exp_time)
            filenames = self._camera.expose(self._exp_time, image_type, 1, broadcast=False).wait()
            self._exposures_done += 1

            # store filename
            return_filenames.append(filenames[0])
            with self._process_lock:
                if self._process_filename is None:
                    self._process_filename = filenames[0]

            # broadcast image path
            if broadcast and self.comm:
                self.comm.send_event(NewImageEvent(filenames[0], image_type))

        # finished
        self._exposure_count = count
        self._exposures_done = 0

        # return filenames
        return return_filenames

    def abort(self, *args, **kwargs):
        """Aborts the current exposure and sequence.

        Returns:
            Success or not.
        """
        self._abort.set()
        self._camera.abort().wait()

    def get_exposure_status(self, *args, **kwargs) -> ICamera.ExposureStatus:
        """Returns the current status of the camera, which is one of 'idle', 'exposing', or 'readout'.

        Returns:
            Current status of camera.
        """
        return self._camera.get_exposure_status().wait()

    def abort_sequence(self, *args, **kwargs):
        """Aborts the current sequence after current exposure.

        Raises:
            ValueError: If sequemce could not be aborted.
        """
        self._exposure_count = None
        self._exposures_done = None
        return self._camera.abort_sequence().wait()

    def get_exposures_left(self, *args, **kwargs) -> int:
        """Returns the remaining exposures.

        Returns:
            Remaining exposures
        """
        if self._exposures_done is None or self._exposure_count is None:
            return 0
        else:
            return self._exposure_count - self._exposures_done

    def get_exposure_time_left(self, *args, **kwargs) -> float:
        """Returns the remaining exposure time on the current exposure in ms.

        Returns:
            Remaining exposure time in ms.
        """
        return self._camera.get_exposures_left().wait()

    def get_exposure_progress(self, *args, **kwargs) -> float:
        """Returns the progress of the current exposure in percent.

        Returns:
            Progress of the current exposure in percent.
        """
        return self._camera.get_exposure_progress().wait()

    def _status_changed(self, event: ExposureStatusChangedEvent, sender: str, *args, **kwargs):
        """Processing status change of camera.

        Args:
            event: Status change event.
            sender: Name of sender.
        """

        # check sender
        if sender == self._camera_name:
            # resend event
            ev = ExposureStatusChangedEvent(last=event.last, current=event.current)
            self.comm.send_event(ev)

    def _process_thread(self):
        """Thread for processing images."""

        # run until closing
        while not self.closing.is_set():
            # do we have an image?
            with self._process_lock:
                filename = self._process_filename

            # got something?
            if filename is not None:
                # download image
                image = self.vfs.download_image(filename)

                # process it
                self._process_image(image)

                # reset image
                with self._process_lock:
                    self._process_filename = None

            # sleep a little
            self.closing.wait(1)

    def _process_image(self, image: Image):
        """Process image.

        Args:
            image: Image to process.
        """

        # find peak count
        peak = self._find_target(image)
        log.info('Found a peak count of %d.', peak)

        # get exposure time from image in ms
        exp_time = image.header['EXPTIME'] * 1000

        # scale exposure time
        exp_time = int(exp_time * self._counts / peak)

        # cut to limits
        self._exp_time = max(min(exp_time, self._max_exp_time), self._min_exp_time)

    def _find_target(self, image: Image) -> int:
        """Find target in image and return it's peak count.

        Args:
            image: Image to analyse.

        Returns:
            Peak count of target.
        """

        # find sources
        sources: pd.DataFrame = self._sep(image).to_pandas()

        # which mode?
        if self._mode == AdaptiveCameraMode.BRIGHTEST:
            # sort by peak brightness and get first
            sources.sort_values('peak', ascending=False, inplace=True)
            return sources['peak'].iloc[0]

        elif self._mode == AdaptiveCameraMode.CENTRE:
            # get image centre
            cx = image.header['CRPIX1'] if 'CRPIX1' in image.header else image.header['NAXIS1'] // 2
            cy = image.header['CRPIX2'] if 'CRPIX2' in image.header else image.header['NAXIS2'] // 2

            # filter all sources within radius around centre
            r = self._radius
            filtered = sources[(cx - r <= sources['x'] <= cx + r) & (cy - r <= sources['y'] <= cy + r)]

            # sort by peak brightness and get first
            filtered.sort_values('peak', ascending=False, inplace=True)
            return filtered['peak'].iloc[0]

        else:
            raise ValueError('Unknown target mode.')

    def get_settings(self, *args, **kwargs) -> dict:
        """Returns a dict of name->type pairs for settings."""
        return {
            'target_counts': 'int',
            'min_exp_time': 'int',
            'max_exp_time': 'int'
        }

    def get_setting_value(self, setting: str, *args, **kwargs):
        """Returns the value of the given setting.

        Args:
            setting: Name of setting

        Returns:
            Current value

        Raises:
            KeyError if setting does not exist
        """
        if setting == 'target_counts':
            return self._counts
        elif setting == 'min_exp_time':
            return self._min_exp_time
        elif setting == 'max_exp_time':
            return self._max_exp_time
        else:
            raise KeyError

    def set_setting_value(self, setting: str, value, *args, **kwargs):
        """Sets the value of the given setting.

        Args:
            setting: Name of setting
            value: New value

        Raises:
            KeyError if setting does not exist
        """
        if setting == 'target_counts':
            self._counts = value
        elif setting == 'min_exp_time':
            self._min_exp_time = value
        elif setting == 'max_exp_time':
            self._max_exp_time = value
        else:
            raise KeyError


__all__ = ['AdaptiveCamera', 'AdaptiveCameraMode']

import logging
import threading

from pyobs import PyObsModule
from pyobs.interfaces import ICamera
from pyobs.modules import timeout
from pyobs.events import NewImageEvent, ExposureStatusChangedEvent
from pyobs.utils.images import Image
from pyobs.utils.photometry import SepPhotometry

log = logging.getLogger(__name__)


class AdaptiveExpTimeCamera(PyObsModule, ICamera):
    """A virtual camera for adaptive exposure times."""

    def __init__(self, camera: str, *args, **kwargs):
        """Creates a new adaptive exposure time cammera.

        Args:
            camera: Actual camera to use.
        """
        PyObsModule.__init__(self, *args, **kwargs)

        # store camera
        self._camera_name = camera
        self._camera = None

        # abort
        self._abort = threading.Event()

        # exposure time
        self._exp_time = None

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

        # loop exposures
        return_filenames = []
        for i in range(count):
            # abort?
            if self._abort.is_set():
                break

            # do exposure(s), never broadcast
            filenames = self._camera.expose(self._exp_time, image_type, 1, broadcast=False).wait()

            # store filename
            return_filenames.append(filenames[0])
            with self._process_lock:
                if self._process_filename is None:
                    self._process_filename = filenames[0]

            # broadcast image path
            if broadcast and self.comm:
                log.info('Broadcasting image ID...')
                self.comm.send_event(NewImageEvent(filenames[0], image_type))

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
        return self._camera.abort_sequence().wait()

    def get_exposures_left(self, *args, **kwargs) -> int:
        """Returns the remaining exposures.

        Returns:
            Remaining exposures
        """
        return self._camera.get_exposures_left().wait()

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
                print('process image ', filename)
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

        # find sources
        sources = self._sep(image)

        # sort by peak brightness
        sources.sort('peak', True)
        print(sources.columns)
        print(sources['peak'])


__all__ = ['AdaptiveExpTimeCamera']
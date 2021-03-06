import datetime
import logging
import math
import os
import threading
from typing import Tuple
import numpy as np
from astropy.io import fits
import astropy.units as u
import yaml
import io

from pyobs.comm import TimeoutException

from pyobs.utils.time import Time
from pyobs.utils.fits import format_filename

from pyobs import PyObsModule
from pyobs.events import NewImageEvent, ExposureStatusChangedEvent, BadWeatherEvent, RoofClosingEvent
from pyobs.interfaces import ICamera, IFitsHeaderProvider, IAbortable
from pyobs.modules import timeout

log = logging.getLogger(__name__)


class CameraException(Exception):
    pass


class BaseCamera(PyObsModule, ICamera, IAbortable):
    def __init__(self, fits_headers: dict = None, centre: Tuple[float, float] = None, rotation: float = None,
                 flip: bool = False,
                 filenames: str = '/cache/pyobs-{DAY-OBS|date:}-{FRAMENUM|string:04d}-{IMAGETYP|type}00.fits.gz',
                 cache: str = '/pyobs/camera_cache.json', fits_namespaces: list = None, *args, **kwargs):
        """Creates a new BaseCamera.

        Args:
            fits_headers: Additional FITS headers.
            centre: (x, y) tuple of camera centre.
            rotation: Rotation east of north.
            flip: Whether or not to flip the image along its first axis.
            filenames: Template for file naming.
            fits_namespaces: List of namespaces for FITS headers that this camera should request
        """
        PyObsModule.__init__(self, *args, **kwargs)

        # check
        if self.comm is None:
            log.warning('No comm module given, will not be able to signal new images!')

        # store
        self._fits_headers = fits_headers if fits_headers is not None else {}
        if 'OBSERVER' not in self._fits_headers:
            self._fits_headers['OBSERVER'] = ['pyobs', 'Name of observer']
        self._centre = centre
        self._rotation = rotation
        self._flip = flip
        self._filenames = filenames
        self._fits_namespaces = fits_namespaces

        # init camera
        self._last_image = None
        self._exposure = None
        self._camera_status = ICamera.ExposureStatus.IDLE
        self._exposures_left = 0
        self._image_type = None

        # multi-threading
        self._expose_lock = threading.Lock()
        self.expose_abort = threading.Event()

        # night exposure number
        self._cache = cache
        self._frame_num = 0

    def open(self):
        """Open module."""
        PyObsModule.open(self)

        # subscribe to events
        if self.comm:
            self.comm.register_event(NewImageEvent)
            self.comm.register_event(ExposureStatusChangedEvent)

            # bad weather
            self.comm.register_event(BadWeatherEvent, self._on_bad_weather)
            self.comm.register_event(RoofClosingEvent, self._on_bad_weather)

    def _change_exposure_status(self, status: ICamera.ExposureStatus):
        """Change exposure status and send event,

        Args:
            status: New exposure status.
        """

        # send event, if it changed
        if self._camera_status != status:
            self.comm.send_event(ExposureStatusChangedEvent(self._camera_status, status))

        # set it
        self._camera_status = status

    def get_exposure_status(self, *args, **kwargs) -> ICamera.ExposureStatus:
        """Returns the current status of the camera, which is one of 'idle', 'exposing', or 'readout'.

        Returns:
            Current status of camera.
        """
        return self._camera_status

    def get_exposure_time_left(self, *args, **kwargs) -> float:
        """Returns the remaining exposure time on the current exposure in ms.

        Returns:
            Remaining exposure time in ms.
        """

        # if we're not exposing, there is nothing left
        if self._exposure is None:
            return 0.

        # calculate difference between start of exposure and now, and return in ms
        diff = self._exposure[0] + datetime.timedelta(milliseconds=self._exposure[1]) - datetime.datetime.utcnow()
        return int(diff.total_seconds() * 1000)

    def get_exposures_left(self, *args, **kwargs) -> int:
        """Returns the remaining exposures.

        Returns:
            Remaining exposures
        """
        return self._exposures_left

    def get_exposure_progress(self, *args, **kwargs) -> float:
        """Returns the progress of the current exposure in percent.

        Returns:
            Progress of the current exposure in percent.
        """

        # if we're not exposing, there is no progress
        if self._exposure is None:
            return 0.

        # calculate difference between start of exposure and now
        diff = datetime.datetime.utcnow() - self._exposure[0]

        # zero exposure time?
        if self._exposure[1] == 0. or self._camera_status == ICamera.ExposureStatus.READOUT:
            return 100.
        else:
            # return max of 100
            percentage = (diff.total_seconds() * 1000. / self._exposure[1]) * 100.
            return min(percentage, 100.)

    def _add_fits_headers(self, hdr: fits.Header):
        """Add FITS header keywords to the given FITS header.

        Args:
            hdr: FITS header to add keywords to.
        """

        # convenience function to return value of keyword
        def v(k):
            return hdr[k][0] if isinstance(k, list) or isinstance(k, tuple) else hdr[k]

        # we definitely need a DATE-OBS and IMAGETYP!!
        if 'DATE-OBS' not in hdr:
            log.warning('No DATE-OBS found in FITS header, adding NO further information!')
            return
        if 'IMAGETYP' not in hdr:
            log.warning('No IMAGETYP found in FITS header, adding NO further information!')
            return

        # get date obs
        date_obs = Time(hdr['DATE-OBS'])

        # UT1-UTC
        hdr['UT1_UTC'] = (float(date_obs.delta_ut1_utc), 'UT1-UTC')

        # basic stuff
        hdr['EQUINOX'] = (2000., 'Equinox of celestial coordinate system')

        # pixel size in world coordinates
        if 'DET-PIXL' in hdr and 'TEL-FOCL' in hdr and 'DET-BIN1' in hdr and 'DET-BIN2' in hdr:
            tmp = 360. / (2. * math.pi) * v('DET-PIXL') / v('TEL-FOCL')
            hdr['CDELT1'] = (-tmp * v('DET-BIN1'), 'Coordinate increment on x-axis [deg/px]')
            hdr['CDELT2'] = (+tmp * v('DET-BIN2'), 'Coordinate increment on y-axis [deg/px]')
            hdr['CUNIT1'] = ('deg', 'Units of CRVAL1, CDELT1')
            hdr['CUNIT2'] = ('deg', 'Units of CRVAL2, CDELT2')
            hdr['WCSAXES'] = (2, 'Number of WCS axes')
        else:
            log.warning('Could not calculate CDELT1/CDELT2 (DET-PIXL/TEL-FOCL/DET-BIN1/DET-BIN2 missing).')

        # do we have a location?
        if self.location is not None:
            loc = self.location
            # add location of telescope
            hdr['LONGITUD'] = (float(loc.lon.degree), 'Longitude of the telescope [deg E]')
            hdr['LATITUDE'] = (float(loc.lat.degree), 'Latitude of the telescope [deg N]')
            hdr['HEIGHT'] = (float(loc.height.value), 'Altitude of the telescope [m]')

            # add local sidereal time
            lst = self.observer.local_sidereal_time(date_obs)
            hdr['LST'] = (lst.to_string(unit=u.hour, sep=':'), 'Local sidereal time')

        # date of night this observation is in
        hdr['DAY-OBS'] = (date_obs.night_obs(self.observer).strftime('%Y-%m-%d'), 'Night of observation')

        # centre pixel
        if self._centre is not None:
            hdr['DET-CPX1'] = (self._centre['x'], 'x-pixel on mechanical axis in unbinned image')
            hdr['DET-CPX2'] = (self._centre['y'], 'y-pixel on mechanical axis in unbinned image')
        else:
            log.warning('Could not calculate DET-CPX1/DET-CPX2 (centre not given in config).')

        # reference pixel in binned image
        if 'DET-CPX1' in hdr and 'DET-BIN1' in hdr and 'DET-CPX2' in hdr and 'DET-BIN2' in hdr:
            # offset?
            off_x, off_y = 0, 0
            if 'XORGSUBF' in hdr and 'YORGSUBF' in hdr:
                off_x = v('XORGSUBF') if 'XORGSUBF' in hdr else 0.
                off_y = v('YORGSUBF') if 'YORGSUBF' in hdr else 0.
            hdr['CRPIX1'] = ((v('DET-CPX1') - off_x) / v('DET-BIN1'), 'Reference x-pixel position in binned image')
            hdr['CRPIX2'] = ((v('DET-CPX2') - off_y) / v('DET-BIN2'), 'Reference y-pixel position in binned image')
        else:
            log.warning('Could not calculate CRPIX1/CRPIX2 '
                            '(XORGSUBF/YORGSUBF/DET-CPX1/TEL-CPX2/DET-BIN1/DET-BIN2) missing.')
        # only add all this stuff for OBJECT images
        if hdr['IMAGETYP'] not in ['dark', 'bias']:
            # projection
            hdr['CTYPE1'] = ('RA---TAN', 'RA in tangent plane projection')
            hdr['CTYPE2'] = ('DEC--TAN', 'Dec in tangent plane projection')

            # PC matrix: rotation only, shift comes from CDELT1/2
            if self._rotation is not None:
                theta_rad = math.radians(self._rotation)
                cos_theta = math.cos(theta_rad)
                sin_theta = math.sin(theta_rad)
                hdr['PC1_1'] = (+cos_theta, 'Partial of first axis coordinate w.r.t. x')
                hdr['PC1_2'] = (-sin_theta, 'Partial of first axis coordinate w.r.t. y')
                hdr['PC2_1'] = (+sin_theta, 'Partial of second axis coordinate w.r.t. x')
                hdr['PC2_2'] = (+cos_theta, 'Partial of second axis coordinate w.r.t. y')
            else:
                log.warning('Could not calculate CD matrix (rotation or CDELT1/CDELT2 missing.')

        # add FRAMENUM
        self._add_framenum(hdr)

    def _add_framenum(self, hdr: fits.Header):
        """Add FRAMENUM keyword to header

        Args:
            hdr: Header to read from and write into.
        """

        # get night from header
        night = hdr['DAY-OBS']

        # increase night exp
        self._frame_num += 1

        # do we have a cache?
        if self._cache is not None:
            # try to load it
            try:
                with self.open_file(self._cache, 'r') as f:
                    cache = yaml.load(f)

                    # get new number
                    if cache is not None and 'framenum' in cache:
                        self._frame_num = cache['framenum'] + 1

                    # if nights differ, reset count
                    if cache is not None and 'night' in cache and night != cache['night']:
                        self._frame_num = 1

            except (FileNotFoundError, ValueError):
                log.warning('Could not read camera cache file.')

            # write file
            try:
                with self.open_file(self._cache, 'w') as f:
                    with io.StringIO() as sio:
                        yaml.dump({'night': night, 'framenum': self._frame_num}, sio)
                        f.write(bytes(sio.getvalue(), 'utf8'))
            except (FileNotFoundError, ValueError):
                log.warning('Could not write camera cache file.')

        # set it
        hdr['FRAMENUM'] = self._frame_num

    def _expose(self, exposure_time: int, open_shutter: bool, abort_event: threading.Event) -> fits.PrimaryHDU:
        """Actually do the exposure, should be implemented by derived classes.

        Args:
            exposure_time: The requested exposure time in ms.
            open_shutter: Whether or not to open the shutter.
            abort_event: Event that gets triggered when exposure should be aborted.

        Returns:
            The actual image.

        Raises:
            ValueError: If exposure was not successful.
        """
        raise NotImplementedError

    def __expose(self, exposure_time: int, image_type: ICamera.ImageType, broadcast: bool) -> (fits.PrimaryHDU, str):
        """Wrapper for a single exposure.

        Args:
            exposure_time: The requested exposure time in ms.
            open_shutter: Whether or not to open the shutter.
            broadcast: Whether or not the new image should be broadcasted.

        Returns:
            Tuple of the image itself and its filename.

        Raises:
            ValueError: If exposure was not successful.
        """
        fits_header_futures = {}
        if self.comm:
            # get clients that provide fits headers
            clients = self.comm.clients_with_interface(IFitsHeaderProvider)

            # create and run a threads in which the fits headers are fetched
            for client in clients:
                log.info('Requesting FITS headers from %s...', client)
                future = self.comm.execute(client, 'get_fits_headers', self._fits_namespaces)
                fits_header_futures[client] = future

        # open the shutter?
        open_shutter = image_type not in [ICamera.ImageType.BIAS, ICamera.ImageType.DARK]

        # do the exposure
        self._exposure = (datetime.datetime.utcnow(), exposure_time)
        try:
            hdu = self._expose(exposure_time, open_shutter, abort_event=self.expose_abort)
            if hdu is None:
                self._exposure = None
                return None, None
        except:
            # exposure was not successful (aborted?), so reset everything
            self._exposure = None
            raise

        # flip it?
        if self._flip:
            hdu.data = np.flip(hdu.data, axis=0)

        # add HDU name
        hdu.name = 'SCI'

        # add image type
        hdu.header['IMAGETYP'] = image_type.value

        # get fits headers from other clients
        for client, future in fits_header_futures.items():
            # join thread
            log.info('Fetching FITS headers from %s...', client)
            try:
                headers = future.wait()
            except TimeoutException:
                log.error('Fetching FITS headers from %s timed out.', client)
                continue

            # add them to fits file
            if headers:
                log.info('Adding additional FITS headers from %s...' % client)
                for key, value in headers.items():
                    # if value is not a string, it may be a list of value and comment
                    if type(value) is list:
                        # convert list to tuple
                        hdu.header[key] = tuple(value)
                    else:
                        hdu.header[key] = value

        # add static fits headers
        for key, value in self._fits_headers.items():
            hdu.header[key] = tuple(value)

        # add more fits headers
        log.info("Adding FITS headers...")
        self._add_fits_headers(hdu.header)

        # don't want to save?
        if self._filenames is None:
            return hdu, None

        # create a temporary filename
        filename = format_filename(hdu.header, self._filenames)
        hdu.header['ORIGNAME'] = (os.path.basename(filename), 'The original file name')
        hdu.header['FNAME'] = (os.path.basename(filename), 'FITS file file name')
        if filename is None:
            raise ValueError('Cannot save image.')

        # upload file
        try:
            with self.open_file(filename, 'wb') as cache:
                log.info('Uploading image to file server...')
                hdu.writeto(cache)
        except FileNotFoundError:
            raise ValueError('Could not upload image.')

        # broadcast image path
        if broadcast and self.comm:
            log.info('Broadcasting image ID...')
            self.comm.send_event(NewImageEvent(filename, image_type))

        # store new last image
        self._last_image = {'filename': filename, 'fits': hdu}

        # return image and unique
        self._exposure = None
        log.info('Finished image %s.', filename)
        return hdu, filename

    @timeout('(exposure_time+30000)*count')
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

        # acquire lock
        log.info('Acquiring exclusive lock on camera...')
        if not self._expose_lock.acquire(blocking=False):
            raise ValueError('Could not acquire camera lock for expose().')

        # make sure that we release the lock
        try:
            # are we exposing?
            if self._camera_status != ICamera.ExposureStatus.IDLE:
                raise CameraException('Cannot start new exposure because camera is not idle.')

            # store type
            self._image_type = image_type

            # loop count
            images = []
            self._exposures_left = count
            while self._exposures_left > 0 and not self.expose_abort.is_set():
                if count > 1:
                    log.info('Taking image %d/%d...', count-self._exposures_left+1, count)

                # expose
                hdu, filename = self.__expose(exposure_time, image_type, broadcast)
                if hdu is None:
                    log.error('Could not take image.')
                else:
                    if filename is None:
                        log.warning('Image has not been saved, so cannot be retrieved by filename.')
                    else:
                        images.append(filename)

                # finished
                self._exposures_left -= 1

            # return id
            self._exposures_left = 0
            return images

        finally:
            # reset type
            self._image_type = None

            # release lock
            log.info('Releasing exclusive lock on camera...')
            self._expose_lock.release()

    def _abort_exposure(self):
        """Abort the running exposure. Should be implemented by derived class.

        Raises:
            ValueError: If an error occured.
        """
        pass

    def abort(self, *args, **kwargs):
        """Aborts the current exposure and sequence.

        Returns:
            Success or not.
        """

        # set abort event
        log.info('Aborting current image and sequence...')
        self._exposures_left = 0
        self.expose_abort.set()

        # do camera-specific abort
        self._abort_exposure()

        # wait for lock and unset event
        acquired = self._expose_lock.acquire(blocking=True, timeout=5.)
        self.expose_abort.clear()
        if acquired:
            self._expose_lock.release()
        else:
            raise ValueError('Could not abort exposure.')

    def abort_sequence(self, *args, **kwargs):
        """Aborts the current sequence after current exposure.

        Returns:
            Success or not.
        """
        if self._exposures_left > 1:
            log.info('Aborting sequence of images...')
        self._exposures_left = 0

    @staticmethod
    def set_biassec_trimsec(hdr: fits.Header, left: int, top: int, width: int, height: int):
        """Calculates and sets the BIASSEC and TRIMSEC areas.

        Args:
            hdr:    FITS header (in/out)
            left:   left edge of data area
            top:    top edge of data area
            width:  width of data area
            height: height of data area
        """

        # get image area in unbinned coordinates
        img_left = hdr['XORGSUBF']
        img_top = hdr['YORGSUBF']
        img_width = hdr['NAXIS1'] * hdr['XBINNING']
        img_height = hdr['NAXIS2'] * hdr['YBINNING']

        # get intersection
        is_left = max(left, img_left)
        is_right = min(left+width, img_left+img_width)
        is_top = max(top, img_top)
        is_bottom = min(top+height, img_top+img_height)

        # for simplicity we allow prescan/overscan only in one dimension
        if (left < is_left or left+width > is_right) and (top < is_top or top+height > is_bottom):
            log.warning('BIASSEC/TRIMSEC can only be calculated with a prescan/overscan on one axis only.')
            return False

        # comments
        c1 = 'Bias overscan area [x1:x2,y1:y2] (binned)'
        c2 = 'Image area [x1:x2,y1:y2] (binned)'

        # rectangle empty?
        if is_right <= is_left or is_bottom <= is_top:
            # easy case, all is BIASSEC, no TRIMSEC at all
            hdr['BIASSEC'] = ('[1:%d,1:%d]' % (hdr['NAXIS1'], hdr['NAXIS2']), c1)
            return

        # we got a TRIMSEC, calculate its binned and windowd coordinates
        is_left_binned = np.floor((is_left - hdr['XORGSUBF']) / hdr['XBINNING']) + 1
        is_right_binned = np.ceil((is_right - hdr['XORGSUBF']) / hdr['XBINNING'])
        is_top_binned = np.floor((is_top - hdr['YORGSUBF']) / hdr['YBINNING']) + 1
        is_bottom_binned = np.ceil((is_bottom - hdr['YORGSUBF']) / hdr['YBINNING'])

        # set it
        hdr['TRIMSEC'] = ('[%d:%d,%d:%d]' % (is_left_binned, is_right_binned, is_top_binned, is_bottom_binned), c2)
        hdr['DATASEC'] = ('[%d:%d,%d:%d]' % (is_left_binned, is_right_binned, is_top_binned, is_bottom_binned), c2)

        # now get BIASSEC -- whatever we do, we only take the last (!) one
        # which axis?
        if img_left+img_width > left+width:
            left_binned = np.floor((is_right - hdr['XORGSUBF']) / hdr['XBINNING']) + 1
            hdr['BIASSEC'] = ('[%d:%d,1:%d]' % (left_binned, hdr['NAXIS1'], hdr['NAXIS2']), c1)
        elif img_left < left:
            right_binned = np.ceil((is_left - hdr['XORGSUBF']) / hdr['XBINNING'])
            hdr['BIASSEC'] = ('[1:%d,1:%d]' % (right_binned, hdr['NAXIS2']), c1)
        elif img_top+img_height > top+height:
            top_binned = np.floor((is_bottom - hdr['YORGSUBF']) / hdr['YBINNING']) + 1
            hdr['BIASSEC'] = ('[1:%d,%d:%d]' % (hdr['NAXIS1'], top_binned, hdr['NAXIS2']), c1)
        elif img_top < top:
            bottom_binned = np.ceil((is_top - hdr['YORGSUBF']) / hdr['YBINNING'])
            hdr['BIASSEC'] = ('[1:%d,1:%d]' % (hdr['NAXIS1'], bottom_binned), c1)

    def _on_bad_weather(self, event, sender: str, *args, **kwargs):
        """Abort exposure if a bad weather event occurs.

        Args:
            event: The bad weather event.
            sender: Who sent it.
        """

        # is current image type one with closed shutter?
        if self._image_type not in [ICamera.ImageType.DARK, ICamera.ImageType.BIAS]:
            # ignore it
            return

        # let's finish current exposure, then abort sequence
        log.warning('Received bad weather event, aborting sequence...')
        self.abort_sequence()


__all__ = ['BaseCamera', 'CameraException']

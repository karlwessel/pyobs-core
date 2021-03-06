import numpy as np
import logging
from scipy import ndimage

from .base import FocusSeries
from ..curvefit import fit_hyperbola
from ..images import Image


log = logging.getLogger(__name__)


class ProjectionFocusSeries(FocusSeries):
    def __init__(self, backsub: bool = True, xbad: list = None, ybad: list = None):
        """Initialize a new projection focus series.

        Args:
            backsub: Do background subtraction?
            xbad: Bad rows
            ybad: Bad columns
        """

        # test imports
        import lmfit

        # stuff
        self._backsub = backsub
        self._xbad = xbad
        self._ybad = ybad
        self._data = []

    def reset(self):
        """Reset focus series."""
        self._data = []

    def analyse_image(self, image: Image):
        """Analyse given image.

        Args:
            image: Image to analyse
        """

        # clean data
        data = self._clean(image.data)

        # get projections
        xproj = np.mean(data, axis=0)
        yproj = np.mean(data, axis=1)
        nx = len(xproj)
        ny = len(yproj)

        # remove background gradient
        xclean = xproj - ndimage.uniform_filter1d(xproj, nx // 10)
        yclean = yproj - ndimage.uniform_filter1d(yproj, ny // 10)

        # get window functions
        xwind = self._window_function(xclean, border=3)
        ywind = self._window_function(yclean, border=3)

        # calculate correlation functions
        xavg = np.average(xclean)
        yavg = np.average(yclean)
        x = xwind * (xclean - xavg) / xavg
        y = ywind * (yclean - yavg) / yavg
        xcorr = np.correlate(x, x, mode='same')
        ycorr = np.correlate(y, y, mode='same')

        # filter out the peak (e.g. cosmics, ...)
        # imx = np.argmax(xcorr)
        # xcorr[imx] = 0.5 * (xcorr[imx - 1] + xcorr[imx + 1])
        # imx = np.argmax(ycorr)
        # ycorr[imx] = 0.5 * (ycorr[imx - 1] + ycorr[imx + 1])

        # fit cc functions to get fwhm
        xfit = self._fit_correlation(xcorr)
        yfit = self._fit_correlation(ycorr)

        # log it
        log.info('Found x=%.1f+-%.1f and y=%.1f+-%.1f.',
                 xfit.params['fwhm'].value, xfit.params['fwhm'].stderr,
                 yfit.params['fwhm'].value, yfit.params['fwhm'].stderr)

        # add to list
        self._data.append({'focus': float(image.header['TEL-FOCU']),
                           'x': float(xfit.params['fwhm'].value), 'xerr': float(xfit.params['fwhm'].stderr),
                           'y': float(yfit.params['fwhm'].value), 'yerr': float(yfit.params['fwhm'].stderr)})

    def fit_focus(self) -> (float, float):
        """Fit focus from analysed images

        Returns:
            Tuple of new focus and its error
        """

        # get data
        focus = [d['focus'] for d in self._data]
        xfwhm = [d['x'] for d in self._data]
        xsig = [d['xerr'] for d in self._data]
        yfwhm = [d['y'] for d in self._data]
        ysig = [d['yerr'] for d in self._data]

        # fit focus
        try:
            xfoc, xerr = fit_hyperbola(focus, xfwhm, xsig)
            yfoc, yerr = fit_hyperbola(focus, yfwhm, ysig)

            # weighted mean
            xerr = np.sqrt(xerr)
            yerr = np.sqrt(yerr)
            foc = (xfoc / xerr + yfoc / yerr) / (1. / xerr + 1. / yerr)
            err = 2. / (1. / xerr + 1. / yerr)
        except (RuntimeError, RuntimeWarning):
            raise ValueError('Could not find best focus.')

        # get min and max foci
        min_focus = np.min(focus)
        max_focus = np.max(focus)
        if foc < min_focus or foc > max_focus:
            raise ValueError("New focus out of bounds: {0:.3f}+-{1:.3f}mm.".format(foc, err))

        # return it
        return float(foc), float(err)

    @staticmethod
    def _window_function(arr, border=0):
        """
        Creates a sine window function of the same size as some 1-D array "arr".
        Optionally, a zero border at the edges is added by "scrunching" the window.
        """
        ndata = len(arr)
        nwind = ndata - 2 * border
        w = np.zeros(ndata)
        for i in range(nwind):
            w[i + border] = np.sin(np.pi * (i + 1.) / (nwind + 1.))
        return w

    @staticmethod
    def _clean(data, backsub=True, xbad=None, ybad=None):
        """
        Removes global slopes and fills up bad rows (ybad) or columns (xbad).
        """
        (ny, nx) = data.shape

        # REMOVE BAD COLUMNS AND ROWS
        if xbad is not None:
            x1 = xbad - 1
            if x1 < 0:
                x1 = 1
            x2 = x1 + 2
            if x2 >= nx:
                x2 = nx - 1
                x1 = x2 - 2
            for j in range(ny):
                data[j][xbad] = 0.5 * (data[j][x1] + data[j][x2])
        if ybad is not None:
            y1 = ybad - 1
            if y1 < 0:
                y1 = 1
            y2 = y1 + 2
            if y2 >= ny:
                y2 = ny - 1
                y1 = y2 - 2
            for i in range(nx):
                data[ybad][i] = 0.5 * (data[y1][i] + data[y2][i])

        # REMOVE GLOBAL SLOPES
        if backsub:
            xsl = np.median(data, axis=0)
            ysl = np.median(data, axis=1).reshape((ny, 1))
            xsl -= np.mean(xsl)
            ysl -= np.mean(ysl)
            xslope = np.tile(xsl, (ny, 1))
            yslope = np.tile(ysl, (1, nx))
            return data - xslope - yslope
        else:
            return data

    @staticmethod
    def _fit_correlation(correl):
        from lmfit.models import GaussianModel

        # create Gaussian model
        model = GaussianModel()

        # initial guess
        x = np.arange(len(correl))
        pars = model.guess(correl, x=x)
        pars['sigma'].value = 20.

        # fit
        return model.fit(correl, pars, x=x)


__all__ = ['ProjectionFocusSeries']

#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
New backend for the Psychtoolbox portaudio engine
"""
from __future__ import absolute_import, division, print_function

import sys
import os
import time
import re
import weakref

from psychopy import prefs, logging, exceptions
from psychopy.constants import (STARTED, PAUSED, FINISHED, STOPPED,
                                NOT_STARTED)
from psychopy.exceptions import SoundFormatError, DependencyError
from ._base import _SoundBase, HammingWindow

try:
    from psychtoolbox import audio
    import psychtoolbox as ptb
except Exception:
    raise DependencyError("psychtoolbox audio failed to import")
try:
    import soundfile as sf
except Exception:
    raise DependencyError("soundfile not working")

import numpy as np

try:
    defaultLatencyClass = int(prefs.hardware['audioLatency'][0])
except (TypeError, IndexError):  # maybe we were given a number instead
    defaultLatencyClass = prefs.hardware['audioLatency']
"""vals in prefs.hardware['audioLatency'] are:
    '0:compatibility'
    '1:balance latency/compatibility'
    '2:prioritise low latency'
    '3:aggressive low-latency'
    '4:critical low-latency'
Based on help at http://psychtoolbox.org/docs/PsychPortAudio-Open
"""
# suggestedLatency = 0.005  ## Not currently used. Keep < 1 scr refresh

if prefs.hardware['audioDriver']=='auto':
    audioDriver = None
else:
    audioDriver = prefs.hardware['audioDriver']

if prefs.hardware['audioDevice']=='auto':
    audioDevice = None
else:
    audioDevice = prefs.hardware['audioDevice']

# these will be used by sound.__init__.py
defaultInput = None
defaultOutput = audioDevice


travisCI = bool(str(os.environ.get('TRAVIS')).lower() == 'true')
logging.info("Loaded psychtoolbox audio version {}"
             .format(audio.get_version_info()['version']))

# ask PTB to align verbosity with our current logging level at console
_verbosities = ((logging.DEBUG, 5),
                (logging.INFO, 4),
                (logging.EXP, 3),
                (logging.WARNING, 2),
                (logging.ERROR, 1))
for _logLevel, _verbos in _verbosities:
    if logging.console.level <= _logLevel:
        audio.verbosity(_verbos)
        break

def init(rate=48000, stereo=True, buffer=128):
    pass  # for compatibility with other backends


def getDevices(kind=None):
    """Returns a dict of dict of audio devices of specified `kind`

    The dict keys are names and items are dicts of properties
    """
    devs = {}
    if travisCI:  # travis-CI testing does not have a sound device
        return devs
    else:
        allDevs = audio.get_devices(kind)
    # annoyingly query_devices is a DeviceList or a dict depending on number
    if type(allDevs) == dict:
        allDevs = [allDevs]
    for ii, dev in enumerate(allDevs):
        # newline characters must be removed
        devName = dev['name'].replace('\r\n', '')
        devs[devName] = dev
        dev['id'] = ii
    return devs


def getStreamLabel(sampleRate, channels, blockSize):
    """Returns the string repr of the stream label
    """
    return "{}_{}_{}".format(sampleRate, channels, blockSize)


class _StreamsDict(dict):
    """Keeps track of what streams have been created. On macOS we can have
    multiple streams under portaudio but under windows we can only have one.

    use the instance `streams` rather than creating a new instance of this
    """

    def getStream(self, sampleRate, channels, blockSize):
        """Gets a stream of exact match or returns a new one
        (if possible for the current operating system)
        """
        # if the query looks flexible then try getSimilar
        if channels == -1 or blockSize == -1:
            return self._getSimilar(sampleRate,
                                    channels=channels,
                                    blockSize=blockSize)
        else:
            return self._getStream(sampleRate,
                                   channels=channels,
                                   blockSize=blockSize)

    def _getSimilar(self, sampleRate, channels=-1, blockSize=-1):
        """Do we already have a compatible stream?

        Many sounds can allow channels and blocksize to change but samplerate
        is generally fixed. Any values set to -1 above will be flexible. Any
        values set to an alternative number will be fixed

        usage:

            label, stream = streams._getSimilar(sampleRate=44100,  # must match
                                               channels=-1,  # any
                                               blockSize=-1)  # wildcard
        """
        label = getStreamLabel(sampleRate, channels, blockSize)
        # replace -1 with any regex integer
        simil = re.compile(label.replace("-1", r"[-+]?(\d+)"))  # I hate REGEX!
        for thisFormat in self:
            if simil.match(thisFormat):  # we found a close-enough match
                return thisFormat, self[thisFormat]
        # if we've been given values in each place then create stream
        if (sampleRate not in [None, -1, 0] and
                channels not in [None, -1] and
                blockSize not in [None, -1]):
            return self._getStream(sampleRate, channels, blockSize)

    def _getStream(self, sampleRate, channels, blockSize):
        """Strict check for this format or create new
        """
        label = getStreamLabel(sampleRate, channels, blockSize)
        # try to retrieve existing stream of that name
        if label in self:
            pass
        # todo: check if this is still needed on win32
        # on some systems more than one stream isn't supported so check
        elif sys.platform == 'win32' and len(self):
            raise exceptions.SoundFormatError(
                "Tried to create audio stream {} but {} already exists "
                "and {} doesn't support multiple portaudio streams"
                    .format(label, list(self.keys())[0], sys.platform)
            )
        else:
            # create new stream
            self[label] = _MasterStream(sampleRate, channels, blockSize,
                                       device=defaultOutput)
        return label, self[label]


streams = _StreamsDict()


class _MasterStream(audio.Stream):
    def __init__(self, sampleRate, channels, blockSize,
                 device=None, duplex=False, mode=1,
                 audioLatencyClass=None):
        # initialise thread
        if audioLatencyClass is None:
            audioLatencyClass = defaultLatencyClass
        self.streamLabel = None
        self.streams = []
        self.list = []
        # sound stream info
        self.sampleRate = sampleRate
        self.channels = 2
        self.duplex = duplex
        self.blockSize = blockSize
        self.label = getStreamLabel(sampleRate, channels, blockSize)
        if device == 'default':
            device = None
        self.sounds = []  # list of dicts for sounds currently playing
        self.takeTimeStamp = False
        self.frameN = 1
        # self.frameTimes = range(5)  # DEBUGGING: store the last 5 callbacks
        if not travisCI:  # travis-CI testing does not have a sound device
            audio.Stream.__init__(self, [], mode=mode+8,
                                  latency_class=audioLatencyClass,
                                  freq=sampleRate, channels=channels,
                                  )  # suggested_latency=suggestedLatency
            self.start(0, 0, 1)
            # self.device = self._sdStream.device
            # self.latency = self._sdStream.latency
            # self.cpu_load = self._sdStream.cpu_load
        self._tSoundRequestPlay = 0


class SoundPTB(_SoundBase):
    """Play a variety of sounds using the new SoundDevice library
    """

    def __init__(self, value="C", secs=0.5, octave=4, stereo=-1,
                 volume=1.0, loops=0,
                 sampleRate=None, blockSize=128,
                 preBuffer=-1,
                 hamming=True,
                 startTime=0, stopTime=-1,
                 name='', autoLog=True):
        """
        :param value: note name ("C","Bfl"), filename or frequency (Hz)
        :param secs: duration (for synthesised tones)
        :param octave: which octave to use for note names (4 is middle)
        :param stereo: -1 (auto), True or False
                        to force sounds to stereo or mono
        :param volume: float 0-1
        :param loops: number of loops to play (-1=forever, 0=single repeat)
        :param sampleRate: sample rate for synthesized tones
        :param blockSize: the size of the buffer on the sound card
                         (small for low latency, large for stability)
        :param preBuffer: integer to control streaming/buffering
                           - -1 means store all
                           - 0 (no buffer) means stream from disk
                           - potentially we could buffer a few secs(!?)
        :param hamming: boolean (default True) to indicate if the sound should
                        be apodized (i.e., the onset and offset smoothly ramped up from
                        down to zero). The function apodize uses a Hanning window, but
                        arguments named 'hamming' are preserved so that existing code
                        is not broken by the change from Hamming to Hanning internally.
                        Not applied to sounds from files.
        :param startTime: for sound files this controls the start of snippet
        :param stopTime: for sound files this controls the end of snippet
        :param name: string for logging purposes
        :param autoLog: whether to automatically log every change
        """
        self.sound = value
        self.name = name
        self.secs = secs  # for any synthesised sounds (notesand freqs)
        self.octave = octave  # for note name sounds
        self.loops = loops
        self._loopsFinished = 0
        self.volume = volume
        self.startTime = startTime  # for files
        self.stopTime = stopTime  # for files specify thesection to be played
        self.blockSize = blockSize  # can be per-sound unlike other backends
        self.preBuffer = preBuffer
        self.frameN = 0
        self._tSoundRequestPlay = 0
        self.sampleRate = sampleRate
        self.channels = None  # let this be set by stereo
        self.stereo = stereo
        self.duplex = None
        self.autoLog = autoLog
        self.streamLabel = ""
        self.sourceType = 'unknown'  # set to be file, array or freq
        self.sndFile = None
        self.sndArr = None
        self.hamming = hamming
        self._hammingWindow = None  # will be created during setSound

        # setSound (determines sound type)
        self.setSound(value, secs=self.secs, octave=self.octave,
                      hamming=self.hamming)
        self.status = NOT_STARTED

    def _getDefaultSampleRate(self):
        """Check what streams are open and use one of these"""
        if len(streams):
            return list(streams.values())[0].sampleRate
        else:
            return 48000  # seems most widely supported

    @property
    def statusDetailed(self):
        if not self.track:
            return None
        return self.track.status

    @property
    def status(self):
        """status gives a simple value from psychopy.constants to indicate
        NOT_STARTED, STARTED, FINISHED, PAUSED

        Psychtoolbox sounds also have a statusDetailed property with further info"""
        if self.__dict__['status']==STARTED:
            # check portaudio to see if still playing
            pa_status = self.statusDetailed
            if not pa_status['Active'] and pa_status['State']==0:
                # we were playing and now not so presumably FINISHED
                self._EOS()

        return self.__dict__['status']


    @status.setter
    def status(self, newStatus):
        self.__dict__['status'] = newStatus

    @property
    def stereo(self):
        return self.__dict__['stereo']

    @stereo.setter
    def stereo(self, val):
        self.__dict__['stereo'] = val
        if val == True:
            self.__dict__['channels'] = 2
        elif val == False:
            self.__dict__['channels'] = 1
        elif val == -1:
            self.__dict__['channels'] = -1

    def setSound(self, value, secs=0.5, octave=4, hamming=None, log=True):
        """Set the sound to be played.

        Often this is not needed by the user - it is called implicitly during
        initialisation.

        :parameters:

            value: can be a number, string or an array:
                * If it's a number between 37 and 32767 then a tone will
                  be generated at that frequency in Hz.
                * It could be a string for a note ('A', 'Bfl', 'B', 'C',
                  'Csh'. ...). Then you may want to specify which octave.
                * Or a string could represent a filename in the current
                  location, or mediaLocation, or a full path combo
                * Or by giving an Nx2 numpy array of floats (-1:1) you can
                  specify the sound yourself as a waveform

            secs: duration (only relevant if the value is a note name or
                a frequency value)

            octave: is only relevant if the value is a note name.
                Middle octave of a piano is 4. Most computers won't
                output sounds in the bottom octave (1) and the top
                octave (8) is generally painful
        """
        # start with the base class method
        _SoundBase.setSound(self, value, secs, octave, hamming, log)

    def _setSndFromFile(self, filename):
        self.sndFile = f = sf.SoundFile(filename)
        self.sourceType = 'file'
        self.sampleRate = f.samplerate
        if self.channels == -1:  # if channels was auto then set to file val
            self.channels = f.channels
        fileDuration = float(len(f)) / f.samplerate  # needed for duration?
        # process start time
        if self.startTime and self.startTime > 0:
            startFrame = self.startTime * self.sampleRate
            self.sndFile.seek(int(startFrame))
            self.t = self.startTime
        else:
            self.t = 0
        # process stop time
        if self.stopTime and self.stopTime > 0:
            requestedDur = self.stopTime - self.t
            self.duration = min(requestedDur, fileDuration)
        else:
            self.duration = fileDuration - self.t
        # can now calculate duration in frames
        self.durationFrames = int(round(self.duration * self.sampleRate))
        # are we preloading or streaming?
        if self.preBuffer == 0:
            # no buffer - stream from disk on each call to nextBlock
            pass
        elif self.preBuffer == -1:
            # full pre-buffer. Load requested duration to memory
            sndArr = self.sndFile.read(
                frames=int(self.sampleRate * self.duration))
            self.sndFile.close()
            self._setSndFromArray(sndArr)
        self._channelCheck(
            self.sndArr)  # Check for fewer channels in stream vs data array


    def _setSndFromArray(self, thisArray):

        self.sndArr = np.asarray(thisArray).astype('float32')
        if thisArray.ndim == 1:
            self.sndArr.shape = [len(thisArray), 1]  # make 2D for broadcasting
        if self.channels == 2 and self.sndArr.shape[1] == 1:  # mono -> stereo
            self.sndArr = self.sndArr.repeat(2, axis=1)
        elif self.sndArr.shape[1] == 1:  # if channels in [-1,1] then pass
            pass
        else:
            try:
                self.sndArr.shape = [len(thisArray), 2]
            except ValueError:
                raise ValueError("Failed to format sound with shape {} "
                                 "into sound with channels={}"
                                 .format(self.sndArr.shape, self.channels))

        # is this stereo?
        if self.stereo == -1:  # auto stereo. Try to detect
            if self.sndArr.shape[1] == 1:
                self.stereo = 0
            elif self.sndArr.shape[1] == 2:
                self.stereo = 1
            else:
                raise IOError("Couldn't determine whether array is "
                              "stereo. Shape={}".format(self.sndArr.shape))
        self._nSamples = thisArray.shape[0]
        if self.stopTime == -1:
            self.stopTime = self._nSamples / float(self.sampleRate)
        # set to run from the start:
        self.seek(0)
        self.sourceType = "array"

        if not self.track:  # do we have one already?
            self.track = audio.Slave(self.stream.handle, data=self.sndArr,
                                     volume=self.volume)
        else:
            self.track.stop()
            self.track.fill_buffer(self.sndArr, start_index=1)

    def _channelCheck(self, array):
        """Checks whether stream has fewer channels than data. If True, ValueError"""
        if self.channels < array.shape[1]:
            msg = (
                "The sound stream is set up incorrectly. You have fewer channels in the buffer "
                "than in data file ({} vs {}).\n**Ensure you have selected 'Force stereo' in "
                "experiment settings**".format(self.channels, array.shape[1]))
            logging.error(msg)
            raise ValueError(msg)

    def play(self, loops=None, when=None):
        """Start the sound playing
        """
        if loops is not None and self.loops != loops:
            self.setLoops(loops)
        self.status = STARTED
        self._tSoundRequestPlay = time.time()
        if hasattr(when, 'getFutureFlipTime'):
            when = when.getFutureFlipTime(ptb=True)
        self.track.start(repetitions=self.loops, when=when)
        # time.sleep(0.)

    def pause(self):
        """Stop the sound but play will continue from here if needed
        """
        self.status = PAUSED
        self.track.stop()

    def stop(self, reset=True):
        """Stop the sound and return to beginning
        """
        self.status = STOPPED
        self.track.stop()
        if reset:
            self.seek(0)

    def seek(self, t):
        self.t = t
        self.frameN = int(round(t * self.sampleRate))
        if self.sndFile and not self.sndFile.closed:
            self.sndFile.seek(self.frameN)

    def _EOS(self, reset=True):
        """Function called on End Of Stream
        """
        self._loopsFinished += 1
        if self.loops == 0:
            self.stop(reset=reset)
        elif self.loops > 0 and self._loopsFinished >= self.loops:
            self.stop(reset=reset)

        self.status = FINISHED

    @property
    def stream(self):
        """Read-only property returns the the stream on which the sound
        will be played
        """
        if not self.streamLabel:
            try:
                label, s = streams.getStream(sampleRate=self.sampleRate,
                                             channels=self.channels,
                                             blockSize=self.blockSize)
            except SoundFormatError as err:
                # try to use something similar (e.g. mono->stereo)
                # then check we have an appropriate stream open
                altern = streams._getSimilar(sampleRate=self.sampleRate,
                                             channels=-1,
                                             blockSize=-1)
                if altern is None:
                    raise SoundFormatError(err)
                else:  # safe to extract data
                    label, s = altern
                # update self in case it changed to fit the stream
                self.sampleRate = s.sampleRate
                self.channels = s.channels
                self.blockSize = s.blockSize
            self.streamLabel = label

        return streams[self.streamLabel]

    def __del__(self):
        if self.track:
            self.track.close()
        self.track = None

    @property
    def track(self):
        """The track on the master stream to which we belong"""
        # the track is actually a weak reference to avoid circularity
        if 'track' in self.__dict__:
            return self.__dict__['track']()
        else:
            return None

    @track.setter
    def track(self, track):
        if track is None:
            self.__dict__['track'] = None
        else:
            self.__dict__['track'] = weakref.ref(track)
#  Copyright (c) 2011, Patrick C. McGinty
#
#  This program is free software: you can redistribute it and/or modify it
#  under the terms of the Simplified BSD License.
#
#  See LICENSE text for more details.
"""
   mktoc.parser
   ~~~~~~~~~~~~

   This module provides object(s) to parse text files describing the layout of
   an audio CD. After the parse step is complete, it is possible to access the
   data or convert into any other output format.

   The following are a list of the classes provided in this module:

   * :class:`ParseData`
   * :class:`CueParser`
   * :class:`WavParser`
"""

from __future__ import absolute_import

from itertools import *
import logging
import operator as op
import os
import re

from .base import *
from . import disc
from . import wav
from . import fsm
from . import progress_bar

__all__ = ['CueParser','WavParser']

log = logging.getLogger('mktoc.parser')


class ParseData(object):
   """
   Stores parsed CD-ROM data and provides methods for modifcation and access.

   Automatically generated by invoking the :meth:`parse` method defined in one
   of the :class:`_Parser` classes.
   """
   def __init__(self, disc, tracks, files):
      """
      Initialize data structures.

      :param disc: CD info data object
      :type  disc: :class:`~mktoc.disc.Disc`

      :param tracks: a lost of objects with track info and indexes for each
                     portion of the track.
      :type  tracks: :func:`list` of class:`disc.Track`

      :param files:  in-order list of WAV files associated with 'tracks'
      :type  files:  :func:`list` of file name str\s
      """
      if len(tracks) == 0:
         raise ParseError()
      self.disc    = disc   # disc object that stores global disc info.
      self._tracks = tracks # track object that stores track info.
      self._files  = files  # in-order list of WAV files that apply to the CD
                            # audio.

   @property
   def last_index(self):
      """Reference to last index of last track."""
      assert self.disc.is_multisession
      return self._tracks[-1].indexes[-1]

   def getToc(self):
      """
      Access method to return a text stream of the CUE data in TOC format.
      """
      toc = []
      toc.extend( str(self.disc).split('\n') )
      for trk in self._tracks:
         toc.extend( str(trk).split('\n') )
      # expand tabs to 4 spaces, strip trailing white space on each line
      toc = [line.expandtabs(4).rstrip() for line in toc]
      return toc

   def modWavOffset(self,samples,tmp=False):
      """
      Optional method to correct the audio WAV data by shifting the samples by
      a positive or negative offset.

      This can be used to compensate for a write offset in a CD/DVD burner. If
      the `tmp` variable is :data:`True`, all new WAV files will be created in
      the :file;`/tmp` directory.

      :param samples:   Number of samples to shift the audio data by. This
                        value can be negative or positive.
      :type  samples:   int

      :param tmp:    :data:`True` or :data:`False`; when :data:`True` any
                     new WAV files will be created in :file:`/tmp`.
      :type tmp:     bool
      """
      # create WavOffset object, initialize sample offset and progress output
      wo = wav.WavOffsetWriter( samples, progress_bar.ProgressBar,
                                   ('processing WAV files:',))
      new_files = wo( self._files, tmp )

      # change all index file names to newly generated files
      file_map = dict( zip(self._files,new_files) )
      indexes = imap(op.attrgetter('indexes'), self._tracks);
      for idx in chain(*indexes):
         if idx.file_: # data tracks do not have valid files
            log.debug( "updating index file '%s'", idx.file_ )
            idx.file_ = file_map[idx.file_]


class _FileLookup(object):
   """
   Return the path to a valid WAV file in the files system using the input
   :param:`file_` value.

   If the WAV file can not be found and :param:`_find_wav` is :data:`True`,
   then an exception is raised.
   """
   # Dictionary to map input WAV files to actual files on the system. The map
   # is for use in cases where the defined file name does not exactly match the
   # file system WAV name.
   _file_map         = None

   # True or flase, when True the WAV file must be found in the FS or an
   # exception is raised.
   _find_wav         = None

   # WavFileCache object that can quickly find WAV files in the local file
   # system.
   _wav_file_cache   = None

   def __init__(self, dir_, find_wav):
      """
      :param dir_:      Path location of the working directory
      :type  dir_:      string

      :param find_wav:  :data:`True`/:data:`False1, :data:`True` causes
                        exceptions to be raised if a WAV file can not be found
                        in the FS.
      :type  find_wav:  bool

      .. Document private members
      .. automethod:: __call__
      """
      # init class options
      self._dir            = dir_
      self._find_wav       = find_wav
      self._file_map       = {}
      assert(dir_)
      self._wav_file_cache = wav.WavFileCache(dir_)

   def __call__(self,file_):
      """
      :param file:   Audio file name parsed from the CUE text.
      :type  file:   string
      """
      if file_ in self._file_map:
         return self._file_map[file_]
      else:
         try:  # attempt to find the WAV file
            file_on_disk = self._wav_file_cache(file_)
         except FileNotFoundError:
            # raise only if '_find_wav' option is True
            if self._find_wav: raise
            else: file_on_disk = file_
         self._file_map[file_] = file_on_disk
         return file_on_disk


class _CueStateMachine(fsm.StateMachine):
   """
   State machine logic for parsing CUE commands in a CUE file.
   """
   #: Regex match pattern for CUE command syntax
   CUE_CMDS = re.compile( r"""
      (?P<catalog>
         ^CATALOG             # CATALOG
            \s+(\d{13})$) |   # value

      (?P<flags>
         ^FLAGS               # FLAG
         \s+(.*)$) |          # one or more flags

      (?P<file>
         ^FILE                # FILE
         \s+"(.*)"            # 'file name' in quotes
         \s+WAVE$) |          # WAVE

      (?P<index>
         ^INDEX                        # INDEX
         \s+(\d+)                      # 'index number'
         \s+(\d{2}:\d{2}:\d{2})$) |    # 'index time'

      (?P<isrc>
         ^ISRC                # ISRC
         \s+(.*)$) |          # value

      (?P<performer>
         ^PERFORMER           # PERFORMER
         \s+"(.*)"$) |        # quoted string

      (?P<pregap>
         ^PREGAP              # PREGAP
         \s+(.*)$) |          # value

      (?P<title>
         ^TITLE               # TITLE
         \s+"(.*)"$) |        # quoted string

      (?P<track>
         ^TRACK                  # TRACK
         \s+(\d+)                # track 'number'
         \s+(AUDIO|MODE.*)$) |   # AUDIO or MODEx/xxxx

      (?P<rem>
         ^REM                 # REM
         \s*(\w*)             # sub-keyword
         \s*(.*))             # remaining text
      """, re.VERBOSE)

   def __init__(self, file_lookup, dir_):
      """
      :param file_lookup:  Callable instance for quickly correlating files in the
                           local file system from file names in CUE commands.
      :type  file_lookup:  :class:`_FileLookup`

      :param dir_:   Path location of the working directory.
      :type  dir_:   str

      .. Document private members
      .. automethod:: __call__
      """
      # callback mapping for 'DISC' state commands
      self.disc_handlers = {
         'catalog'      : self.cmd_field_disc,
         'file'         : self.cmd_file,
         'performer'    : self.cmd_field_disc,
         'rem'          : self.cmd_rem,
         'title'        : self.cmd_field_disc,
         }
      # callback mapping for 'FILE' state commands
      self.file_handlers = {
         'file'         : self.cmd_file,
         'index'        : self.cmd_index,
         'track'        : self.cmd_track,
         }
      # callback mapping for 'TRACK' state commands
      self.track_handlers = {
         'file'         : self.cmd_file,
         'flags'        : self.cmd_flags,
         'index'        : self.cmd_index,
         'isrc'         : self.cmd_field_trk,
         'performer'    : self.cmd_field_trk,
         'pregap'       : self.cmd_field_trk,
         'rem'          : self.cmd_noop,
         'title'        : self.cmd_field_trk,
         'title'        : self.cmd_field_trk,
         'track'        : self.cmd_track,
         }
      # instance variables for managing parsing logic
      self.disc   = disc.Disc()
      self.tracks = []
      self.track  = None
      self.files  = []
      self.file_  = None
      self.file_lookup = file_lookup
      self.dir_   = dir_
      # initialize beginning state
      self.change_state( self.CUE_CMDS, self.disc_handlers )

   def __call__(self,*a,**kw):
      """
      Extends the super class method by catching 'KeyErrors' caused by
      unexpected or unmatched patterns.
      """
      try:
         super(_CueStateMachine,self).__call__(*a,**kw)
      except (fsm.NullStateException,) as e:
         raise ParseError( 'Unknown/invalid command: ' + str(e) )
      return ParseData(self.disc, self.tracks, self.files)

   def cmd_noop( self, match_name, cmd, *args ):
      """Ignored commands"""

   def cmd_rem( self, match_name, cmd, field, value):
      """
      Store a REM field in the disc data, unhandled fields will be silently
      ignored.
      """
      self.disc.set_field( field, value)

   def cmd_field_disc( self, match_name, cmd, value):
      """Store a command field in the disc data."""
      self.disc.set_field( match_name, value)

   def cmd_field_trk( self, match_name, cmd, value):
      """Store a command field in the track data."""
      self.track.set_field( match_name, value)

   def cmd_file( self, match_name, cmd, file_):
      """Process a new data file name. Changes state to 'FILE'."""
      self.file_ = self.file_lookup(file_)
      self.files.append( self.file_ )
      self.change_state( match_handlers=self.file_handlers )  # next state

   def cmd_track( self, match_name, cmd, trk_num, trk_type):
      """Create a new :class:`~mktoc.disc.Track` instance.
      Change state to 'TRACK'.
      """
      self.track = disc.Track(int(trk_num), trk_type != 'AUDIO')
      self.tracks.append( self.track )
      if trk_type != 'AUDIO':
         self.disc.is_multisession = True    # disc is multi-session
      self.change_state( match_handlers=self.track_handlers ) # next state

   def cmd_index( self, match_name, cmd, idx_num, time):
      """
      Create a new :class:`~mktoc.disc.TrackIndex` instance.

      Additional processing steps are performed here to modify the state of
      previous data structures.
      """
      if not self.track.is_data:
         idx = disc.TrackIndex( idx_num, time, self.file_)
      else:
         # if data track, the length is not defined in the CUE and must be
         # sourced from another method to create a 100% accurate TOC
         size = self.data_trk_size(self.track.num)
         if not size:
            # TODO: the user should be allowed to override this error in the
            # future, and/or have feature to manually set the track size
            raise ParseError('size of DATA track can not be determined')
         idx = disc.TrackIndex( idx_num, time, None, size)
         idx.cmd = disc.TrackIndex.DATA

      self.track.indexes.append( idx )

      # set local var defaults
      prev_idx = None
      if len(self.track.indexes) >= 2:
         prev_idx = self.track.indexes[-2] # [-1] is current index
      prev_trk = None
      if len(self.tracks) >= 2:
         prev_trk = self.tracks[-2] # [-1] is current track

      # Add 'START' command after pregap audio file
      #
      # if 'prev_idx' is a track pregap (num == 0) and the file for 'current
      # index' is not the same, then designate the 'prev_idx' as a pregap audio
      # only. The result is to place a TOC 'START' command between the
      # 'prev_idx' and the 'current index' in the TOC file.
      if prev_idx and prev_idx.num == 0 and idx.file_ != prev_idx.file_:
         prev_idx.cmd = disc.TrackIndex.PREAUDIO

      # When a single WAV file is used for multiple internal track indexes:
      if prev_idx and self.file_ == prev_idx.file_:
         if prev_idx.num == 0:
            # Designate the 'true' start index of a track when the track data
            # file contains pregap data. This is done with the TOC command
            # 'START'
            #
            # details:  if the current index is the pregap data (0), then the
            #           pregap must be set by changing the 'next' index cmd to
            #           'START', and the length of the pregap must be set.
            idx.cmd = disc.TrackIndex.START
            idx.len_ = idx.time - prev_idx.time
            del idx.time # remove for safety, do not use
         else:
            # Else not a pregap, change the TOC command for a new track to
            # 'INDEX' when a single logical 'track' has multiple index values
            # (by default, the TOC command is AUDIOFILE when a track has a
            # single index).
            #
            # details:  the outside 'if' guarantee that the current and next
            #           index use the same file. Also, since it is not a
            #           pregap the TOC format must use 'INDEX' keyword
            #           instead of AUDIOFILE. No other calculations are
            #           needed because INDEX is specified by file offset.
            idx.cmd = disc.TrackIndex.INDEX
            del idx.len_ # remove for safety, do not use

      # Set the LENGTH argument on a track fle that must stop before EOF
      #
      # On the current index, which is the first index of track 2 or
      # greater,
      if prev_trk and len(self.track.indexes) == 1:
         for prev_idx in prev_trk.indexes:
            # if TOC command for previous track index is AUDIOFILE, and if prev
            # track uses the same file, then prev INDEX must end before the
            # current track INDEX starts.
            if (prev_idx.cmd == disc.TrackIndex.AUDIO
                  and prev_idx.file_ == self.file_):
               prev_idx.len_ = idx.time - prev_idx.time

   def cmd_flags( self, match_name, cmd, flags):
      """Set the state of flag fields in a :class:`disc.Track` instance."""
      for f in filter(lambda x: x in ['DCP','4CH','PRE'],flags.split()):
         if f == '4CH': f = 'four_ch'     # change '4CH' flag name
         self.track.set_field(f,True)

   def data_trk_size(self, trk_idx):
      """
      Use an ExactAudioCopy log file to determine the length of the track at
      the specified index.

      :param trk_idx: Track index of data
      :type  trk_idx: int
      """
      import codecs
      import chardet.universaldetector
      size = None
      files = os.listdir(self.dir_)
      logs = [f for f in files if os.path.splitext(f)[1] == '.log']
      logs.sort()
      for f in logs:
         # detect file character encoding
         with open(os.path.join(self.dir_,f),'rb') as fh:
            d = chardet.universaldetector.UniversalDetector()
            for line in fh.readlines():
               d.feed(line)
            d.close()
            encoding = d.result['encoding']
         with codecs.open( os.path.join(self.dir_,f),
                           'rb', encoding=encoding) as fh:
            lines = fh.readlines()
         regex = re.compile(r'^\s+%d\s+\|.+\|\s+(.+)\s+\|.+\|.+$' % (trk_idx,))
         matches = filter(None,map(regex.match,lines))
         if matches:
            # convert first match from '1:11.11' to '1:11:11'
            size = matches[0].group(1).replace('.',':')
            break

      return size


class CueParser(object):
   """
   An audio CUE sheet text file parsing class.

   By matching the known format of a CUE file, the relevant text information is
   extracted and converted to a binary representation. The binary
   representation is created by using combination of Disc, Track, and
   TrackIndex objects. With the data, the CUE file can be re-created or
   converted into a new format.
   """
   def __init__(self, dir_=os.curdir, find_wav=True):
      """
      :param dir_:  Path location of the CUE file's directory.
      :type  dir_:  str

      :param find_wav:  :data:`True`/:data:`False`, :data:`True` causes
                        exceptions to be raised if a WAV file can not be found
                        in the FS.
      :type  find_wav:  bool
      """
      self.dir_ = dir_
      self.file_lookup = _FileLookup(dir_,find_wav)

   def parse(self, fh):
      """
      Parses CUE file text data.

      :param fh:  An open file handle used to read the CUE text data
      :type fh:   :data:`file`

      :returns: :class:`ParseData` instance that mirrors the CUE data.
      """
      # parse disc into memory, ignore comments
      cue = [line.strip() for line in fh]
      if not len(cue):
         raise EmptyCueData
      # begin state machine in 'Init' state
      csm = _CueStateMachine(self.file_lookup, self.dir_)
      return csm( cue )


class WavParser(object):
   """
   A simple parser object that uses a list of WAV files to create a CD TOC.

   The class assumes that each WAV file is an individual track, in ascending
   order.
   """
   def __init__(self, dir_=os.curdir, find_wav=True):
      """
      :param dir_:  Path location of the CUE file's directory.
      :type  dir_:  str

      :param find_wav:  :data:`True`/:data:`False`, :data:`True` causes
                        exceptions to be raised if a WAV file can not be found
                        in the FS.
      :type find_wav: bool
      """
      # init class options
      self.file_lookup = _FileLookup(dir_,find_wav)

   def parse( self, wav_files):
      """
      Parses a list of WAV files.

      :param wav_files: WAV files to add to the TOC
      :type  wav_files: list

      :returns: :class:`ParseData` instance that mirrors the WAV data.
      """
      files = map(self.file_lookup, wav_files)
      # return a new Track object with a single Index using 'file_'
      def mk_track((idx,file_)):
         # create a new track for the WAV file
         trk = disc.Track(idx+1)
         # add the WAV file to the first index in the track
         trk.indexes.append( disc.TrackIndex(1,0,file_) )
         return trk
      # return a new ParseData object with empy Disc and complete Track list
      return ParseData( disc.Disc(),
                        map( mk_track, enumerate(files)), files )


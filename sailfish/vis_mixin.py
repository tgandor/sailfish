"""Auxilliary classes for on-line data visualization."""

__author__ = 'Michal Januszewski'
__email__ = 'sailfish-cfd@googlegroups.com'
__license__ = 'LGPL3'

import hashlib
import os
import subprocess
import zlib
from sailfish import lb_base
from sailfish.util import ArrayPair
import numpy as np

import netifaces
import zmq


class Slice(object):
    def __init__(self, pair, kernel, min_arg):
        self.pair = pair
        self.kernel = kernel
        self.min_arg = min_arg


class VisConfig(object):
    """Container class for settings related to visualization."""
    def __init__(self):
        self.every = 100
        self.axis = 0
        self.position = 0
        self.field = 0
        self.levels = 256


class Vis2DSliceMixIn(lb_base.LBMixIn):
    """Extracts 2D slices of 3D fields for on-line visualization."""
    aux_code = ['data_processing.mako']

    @classmethod
    def add_options(cls, group, dim):
        group.add_argument('--visualizer_port', type=int, default=0,
                           help='Base port to use for visualization. '
                           'The subdomain ID will be added to it to '
                           'generate the actual port number.')
        group.add_argument('--visualizer_data_port', type=int, default=0,
                           help='Port to use for visualization data. '
                           'The subdomain ID will be added to it to '
                           'generate the actual port number.')
        group.add_argument('--visualizer_auth_token', type=str, default='',
                           help='Authentication token for control of the '
                           'visualizer. If empty, a token will be generated '
                           'automatically.')
        group.add_argument('--visualizer_start', action='store_true',
                           default=False, help='Automatically start the '
                           'visualizer frontend.')

    def before_main_loop(self, runner):
        self._vis_config = VisConfig()
        self._ctx = zmq.Context()
        self._sock = self._ctx.socket(zmq.XPUB)
        self._ctrl_sock = self._ctx.socket(zmq.REP)

        if runner.config.visualizer_data_port > 0:
            self._port = runner.config.visualizer_data_port
            self._sock.bind('tcp://*:{0}'.format(self._port))
        else:
            self._port = self._sock.bind_to_random_port('tcp://*')

        if runner.config.visualizer_auth_token:
            self._authtoken = runner.config.visualizer_auth_token
        else:
            self._authtoken = hashlib.md5(np.random.rand(100)).hexdigest()

        if runner.config.visualizer_port > 0:
            self._ctrl_sock.bind('tcp://*:{0}'.format(runner.config.visualizer_port))
            self._ctrl_port = runner.config.visualizer_port
        else:
            self._ctrl_port = self._ctrl_sock.bind_to_random_port('tcp://*')

        server_url = ''
        for iface in netifaces.interfaces():
            addr = netifaces.ifaddresses(iface).get(
                netifaces.AF_INET, [{}])[0].get('addr', '')
            if addr:
                server_url = 'tcp://%s@%s:%d' % (self._authtoken, addr,
                                                 self._ctrl_port)
                self.config.logger.info('Visualization server at %s',
                                        server_url)

        gpu_v = runner.gpu_field(self.v)

        self._buf_sizes = (runner._spec.ny * runner._spec.nz,
                           runner._spec.nx * runner._spec.nz,
                           runner._spec.nx * runner._spec.ny)
        self._buf_shapes = ((runner._spec.ny, runner._spec.nz),
                            (runner._spec.nx, runner._spec.nz),
                            (runner._spec.nx, runner._spec.ny))
        self._axis_len = (runner._spec.nx, runner._spec.ny, runner._spec.nz)

        targets = [self.vx, self.vy, self.vz]
        targets.extend(self._scalar_fields)
        self._names = ['vx', 'vy', 'vz']

        gpu_targets = list(gpu_v)
        for f in self._scalar_fields:
            gpu_targets.append(runner.gpu_field(f.buffer))
            self._names.append(f.abstract.name)

        self._vis_gpu_targets = gpu_targets

        # The buffer has to be large enough to hold any slice.
        def _make_buf(size):
            h = np.zeros(size, dtype=runner.float)
            h[:] = np.nan
            return ArrayPair(h, runner.backend.alloc_buf(like=h))

        self._vis_pairs = []
        buffer_size = max(self._buf_sizes)
        for gf in self._vis_gpu_targets:
            pair = _make_buf(buffer_size)
            self._vis_pairs.append(pair)

        self._vis_update_kernels(runner)
        self._num_subs = 0

        self._poller = zmq.Poller()
        self._poller.register(self._ctrl_sock, zmq.POLLIN)

        if server_url and self.config.visualizer_start:
            self.config.logger.info('Starting visualizer..')
            path = os.path.realpath(os.path.join(os.path.dirname(
                lb_base.__file__), '../utils'))
            subprocess.Popen([os.path.join(path, 'visualizer.py'), server_url])

    def _vis_update_kernels(self, runner):
        gpu_map = runner.gpu_geo_map()
        self._slices = []

        base_args = []
        base_sig = ''
        min_arg = 0
        if self.config.node_addressing == 'indirect':
            base_args.append(runner.gpu_indirect_address())
            base_sig += 'P'
            min_arg = 1

        for gf, pair in zip(self._vis_gpu_targets, self._vis_pairs):
            self._slices.append(
                Slice(pair, runner.get_kernel(
                    'ExtractSliceField',
                    base_args + [self._vis_config.axis,
                                 self._vis_config.position, gpu_map, gf,
                                 pair.gpu],
                    base_sig + 'iiPPP'), min_arg))

    def _handle_commands(self):
        socks = dict(self._poller.poll(0))
        if self._ctrl_sock in socks:
            cmd = self._ctrl_sock.recv_json()
            ack = True
            self.config.logger.debug('Visualizer command "%s" received.' %
                                     cmd[1])
            if cmd[0] != self._authtoken:
                self._ctrl_sock.send('unauthorized')
                return True

            if cmd[1] == 'axis':
                self._vis_config.axis = cmd[2]
            elif cmd[1] == 'position':
                self._vis_config.position = cmd[2]
            elif cmd[1] == 'field':
                self._vis_config.field = cmd[2]
            elif cmd[1] == 'every':
                self._vis_config.every = cmd[2]
            elif cmd[1] == 'levels':
                self._vis_config.levels = cmd[2]
            elif cmd[1] == 'port_info':
                self._ctrl_sock.send_json((self._port, self._vis_config.__dict__))
                ack = False
            else:
                self.config.logger.info('Invalid visualizer command: %s' %
                                        cmd[1])

            if ack:
                self._ctrl_sock.send('ack')
            return True
        # Stop processing on unknown commands or when there
        # are no outstanding requests.
        else:
            return False
        return False

    def after_step(self, runner):
        while self._handle_commands():
            pass

        mod = self.iteration % self._vis_config.every
        if mod == self._vis_config.every - 1:
            self.need_fields_flag = True
        elif mod == 0:
            def _swap(shape):
                return shape[1], shape[0]

            # Collect all data into a single dict.
            md = {
                'axis_range': self._axis_len[self._vis_config.axis],
                'axis': self._vis_config.axis,
                'current': (self._vis_config.axis, self._vis_config.position),
                'every': self._vis_config.every,
                'field': self._vis_config.field,
                'iteration': self.iteration,
                'names': self._names,
                'dtype': str(np.float16().dtype),
                'shape': _swap(self._buf_shapes[self._vis_config.axis])
            }

            # Look for new subscribers.
            try:
                rc = self._sock.recv(zmq.NOBLOCK)
                if rc[0] == '\x01':
                    self._num_subs += 1
                    self.config.logger.info('Visualizer connected.')
                else:
                    self._num_subs -= 1
                    self.config.logger.info('Visualizer disconnected.')
            except zmq.ZMQError:
                pass

            # Nothing to do if there is no one to receive the update on the
            # other end.
            if self._num_subs <= 0:
                return

            # Send the metadata first.
            try:
                self._sock.send_json(md, zmq.SNDMORE | zmq.NOBLOCK)
            except zmq.ZMQError:
                # This most likely indicates that the socket is not connected.
                # Bail out early to avoid running kernels to extract slice data.
                return

            shape = self._buf_shapes[self._vis_config.axis]
            grid = [(shape[0] + self.config.block_size - 1) //
                    self.config.block_size, shape[1]]

            # Run kernel to extract slice data.
            sl = self._slices[self._vis_config.field]
            sl.kernel.args[sl.min_arg] = self._vis_config.axis
            sl.kernel.args[sl.min_arg + 1] = self._vis_config.position
            runner.backend.run_kernel(sl.kernel, grid)

            # Selector to extract part of slice buffer that actually holds
            # meaningful data. We need this since we use a single buffer
            # for slices of all three possible orientations.
            selector = slice(0, self._buf_sizes[self._vis_config.axis])
            runner.backend.from_buf(sl.pair.gpu)
            try:
                data = sl.pair.host[selector]
                # Bucketize the data to improve compression rates.
                baseline = np.nanmin(data)
                dv = ((np.nanmax(data) - np.nanmin(data)) /
                      self._vis_config.levels)
                data = np.round((data - baseline) / dv)
                data *= dv
                data += baseline
                self._sock.send(zlib.compress(data.astype(np.float16)), zmq.NOBLOCK)
            except zmq.ZMQError:
                self.config.logger.error('Failed to send visualization data')
                return

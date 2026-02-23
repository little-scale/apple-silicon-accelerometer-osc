#!/usr/bin/env python3
"""
OSC bridge for apple-silicon-accelerometer

Reads sensor data from the shared memory ring buffers created by
motion_live.py (or a standalone sensor_worker) and forwards everything
as OSC messages over UDP.

Run motion_live.py (or your own sensor process) first, then:
    python3 osc_bridge.py                          # defaults: 127.0.0.1:9000
    python3 osc_bridge.py --ip 10.0.0.5 --port 7400
    python3 osc_bridge.py --bundle              # pack per-tick into bundles
    python3 osc_bridge.py --rate 60                 # limit to 60 msgs/s
    python3 osc_bridge.py --no-gyro --no-als        # skip sensors you don't need

OSC address map:
    /accel      f f f       x y z  (g)
    /gyro       f f f       x y z  (deg/s)
    /accel/mag  f           magnitude (g, gravity removed via HP filter)
    /lid        f           angle (degrees)
    /als/lux    f           ambient light intensity (0-1)
    /als/spec   i i i i     raw spectral channel counts

Designed to sit alongside the existing codebase without modifying it.
Requires: python-osc  (pip install python-osc)
"""

import argparse
import math
import struct
import sys
import time
import multiprocessing.shared_memory

from pythonosc import osc_message_builder, osc_bundle_builder, udp_client

from spu_sensor import (
    shm_read_new, shm_read_new_gyro, shm_snap_read,
    SHM_NAME, SHM_NAME_GYRO, SHM_SIZE,
    SHM_NAME_ALS, SHM_ALS_SIZE, SHM_NAME_LID, SHM_LID_SIZE,
    ALS_REPORT_LEN, ACCEL_SCALE,
    SHM_HEADER, RING_ENTRY, RING_CAP, SHM_SNAP_HDR,
)


# ALS report field offsets (from motion_live.py)
_ALS_SPEC_OFFSETS = [20, 24, 28, 32]
_ALS_LUX_OFF = 40


def parse_als(raw):
    """Extract lux + spectral channels from a raw ALS report."""
    if raw is None or len(raw) < 44:
        return None, None
    lux = struct.unpack_from('<f', raw, _ALS_LUX_OFF)[0]
    channels = [struct.unpack_from('<I', raw, o)[0] for o in _ALS_SPEC_OFFSETS]
    return lux, channels


def try_attach_shm(name):
    """Attach to existing shared memory, return (shm, buf) or (None, None)."""
    try:
        shm = multiprocessing.shared_memory.SharedMemory(name=name, create=False)
        return shm, shm.buf
    except FileNotFoundError:
        return None, None


def main():
    ap = argparse.ArgumentParser(
        description='OSC bridge for apple-silicon-accelerometer sensors')
    ap.add_argument('--ip', default='127.0.0.1', help='OSC target IP (default 127.0.0.1)')
    ap.add_argument('--port', type=int, default=9000, help='OSC target port (default 9000)')
    ap.add_argument('--rate', type=float, default=100.0,
                    help='Max OSC messages per second (default 100, 0 = unlimited)')
    ap.add_argument('--bundle', action='store_true',
                    help='Send each tick as an OSC bundle instead of individual messages')
    ap.add_argument('--no-accel', action='store_true', help='Skip accelerometer')
    ap.add_argument('--no-gyro', action='store_true', help='Skip gyroscope')
    ap.add_argument('--no-als', action='store_true', help='Skip ambient light sensor')
    ap.add_argument('--no-lid', action='store_true', help='Skip lid angle sensor')
    ap.add_argument('--prefix', default='', help='OSC address prefix (e.g. /macbook)')
    ap.add_argument('--verbose', '-v', action='store_true', help='Print OSC messages to terminal')
    args = ap.parse_args()

    prefix = args.prefix.rstrip('/')

    # --- attach to shared memory segments ---
    accel_shm, accel_buf = (None, None) if args.no_accel else try_attach_shm(SHM_NAME)
    gyro_shm, gyro_buf = (None, None) if args.no_gyro else try_attach_shm(SHM_NAME_GYRO)
    als_shm, als_buf = (None, None) if args.no_als else try_attach_shm(SHM_NAME_ALS)
    lid_shm, lid_buf = (None, None) if args.no_lid else try_attach_shm(SHM_NAME_LID)

    attached = []
    if accel_buf is not None: attached.append('accel')
    if gyro_buf is not None: attached.append('gyro')
    if als_buf is not None: attached.append('als')
    if lid_buf is not None: attached.append('lid')

    if not attached:
        print('[!] No shared memory segments found. Is motion_live.py running?',
              file=sys.stderr)
        sys.exit(1)

    print(f'[osc_bridge] sensors: {", ".join(attached)}')
    print(f'[osc_bridge] sending to {args.ip}:{args.port}  '
          f'rate={args.rate}hz  bundle={args.bundle}  prefix="{prefix}"')

    client = udp_client.SimpleUDPClient(args.ip, args.port)

    # high-pass filter state for accel magnitude (gravity removal)
    hp_alpha = 0.95
    hp_prev_raw = [0.0, 0.0, 0.0]
    hp_prev_out = [0.0, 0.0, 0.0]
    hp_ready = False

    last_accel_total = 0
    last_gyro_total = 0
    last_als_count = 0
    last_lid_count = 0

    min_dt = 1.0 / args.rate if args.rate > 0 else 0.0
    last_send = 0.0
    msg_count = 0

    try:
        while True:
            now = time.time()

            # rate limiting
            if min_dt > 0 and (now - last_send) < min_dt:
                time.sleep(max(0.001, min_dt - (now - last_send)))
                now = time.time()

            bundle = None
            if args.bundle:
                bundle = osc_bundle_builder.OscBundleBuilder(
                    osc_bundle_builder.IMMEDIATELY)

            sent_this_tick = False

            # --- accelerometer ---
            if accel_buf is not None:
                samples, last_accel_total = shm_read_new(accel_buf, last_accel_total)
                if samples:
                    # send the latest sample (or you could send all for max resolution)
                    ax, ay, az = samples[-1]

                    # high-pass for dynamic magnitude
                    if not hp_ready:
                        hp_prev_raw = [ax, ay, az]
                        hp_prev_out = [0.0, 0.0, 0.0]
                        hp_ready = True
                        dyn_mag = 0.0
                    else:
                        hx = hp_alpha * (hp_prev_out[0] + ax - hp_prev_raw[0])
                        hy = hp_alpha * (hp_prev_out[1] + ay - hp_prev_raw[1])
                        hz = hp_alpha * (hp_prev_out[2] + az - hp_prev_raw[2])
                        hp_prev_raw = [ax, ay, az]
                        hp_prev_out = [hx, hy, hz]
                        dyn_mag = math.sqrt(hx*hx + hy*hy + hz*hz)

                    if bundle:
                        msg = osc_message_builder.OscMessageBuilder(
                            address=f'{prefix}/accel')
                        msg.add_arg(ax); msg.add_arg(ay); msg.add_arg(az)
                        bundle.add_content(msg.build())

                        msg2 = osc_message_builder.OscMessageBuilder(
                            address=f'{prefix}/accel/mag')
                        msg2.add_arg(dyn_mag)
                        bundle.add_content(msg2.build())
                    else:
                        client.send_message(f'{prefix}/accel', [ax, ay, az])
                        client.send_message(f'{prefix}/accel/mag', [dyn_mag])

                    sent_this_tick = True

            # --- gyroscope ---
            if gyro_buf is not None:
                gyro_samples, last_gyro_total = shm_read_new_gyro(
                    gyro_buf, last_gyro_total)
                if gyro_samples:
                    gx, gy, gz = gyro_samples[-1]

                    if bundle:
                        msg = osc_message_builder.OscMessageBuilder(
                            address=f'{prefix}/gyro')
                        msg.add_arg(gx); msg.add_arg(gy); msg.add_arg(gz)
                        bundle.add_content(msg.build())
                    else:
                        client.send_message(f'{prefix}/gyro', [gx, gy, gz])

                    sent_this_tick = True

            # --- ambient light ---
            if als_buf is not None:
                als_data, last_als_count = shm_snap_read(
                    als_buf, last_als_count, ALS_REPORT_LEN)
                if als_data is not None:
                    lux, channels = parse_als(als_data)
                    if lux is not None:
                        if bundle:
                            msg = osc_message_builder.OscMessageBuilder(
                                address=f'{prefix}/als/lux')
                            msg.add_arg(float(lux))
                            bundle.add_content(msg.build())

                            msg2 = osc_message_builder.OscMessageBuilder(
                                address=f'{prefix}/als/spec')
                            for ch in channels:
                                msg2.add_arg(ch)
                            bundle.add_content(msg2.build())
                        else:
                            client.send_message(f'{prefix}/als/lux', [float(lux)])
                            client.send_message(f'{prefix}/als/spec', channels)

                        sent_this_tick = True

            # --- lid angle ---
            if lid_buf is not None:
                lid_data, last_lid_count = shm_snap_read(
                    lid_buf, last_lid_count, 4)
                if lid_data is not None:
                    angle = struct.unpack('<f', lid_data)[0]

                    if bundle:
                        msg = osc_message_builder.OscMessageBuilder(
                            address=f'{prefix}/lid')
                        msg.add_arg(float(angle))
                        bundle.add_content(msg.build())
                    else:
                        client.send_message(f'{prefix}/lid', [float(angle)])

                    sent_this_tick = True

            # --- send bundle ---
            if bundle and sent_this_tick:
                client.send(bundle.build())

            if sent_this_tick:
                last_send = now
                msg_count += 1
                if args.verbose and msg_count % 50 == 0:
                    ax, ay, az = samples[-1] if (accel_buf is not None and samples) else (0,0,0)
                    print(f'\r[{msg_count}] accel: {ax:+.4f} {ay:+.4f} {az:+.4f}  ', end='')

            if not sent_this_tick:
                time.sleep(0.002)

    except KeyboardInterrupt:
        print(f'\n[osc_bridge] sent {msg_count} messages, exiting')
    finally:
        for shm in (accel_shm, gyro_shm, als_shm, lid_shm):
            if shm is not None:
                shm.close()


if __name__ == '__main__':
    main()

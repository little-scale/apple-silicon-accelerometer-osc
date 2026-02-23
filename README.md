# apple-silicon-accelerometer-osc

To be used within https://github.com/olvvier/apple-silicon-accelerometer in order to send OSC messages to other places

OSC address map:
    /accel      f f f       x y z  (g)
    /gyro       f f f       x y z  (deg/s)
    /accel/mag  f           magnitude (g, gravity removed via HP filter)
    /lid        f           angle (degrees)
    /als/lux    f           ambient light intensity (0-1)
    /als/spec   i i i i     raw spectral channel counts

# Multi-rig multi-application hacks for Windows

These are a collection of things I wrote for myself to glue various bits
together for my ham station. I'm posting here in case they are helpful to
others, but mostly to document them for myself.

I am not a Windows guy, but recognize that I kinda need to use it for Ham and
contesting stuff. A lot of this could be done with shell scripts on a POSIX
machine, but it is much harder on Windows. The intent here was to make as much
of this infrastructure sit in the background as Windows services and just
always "work" requiring no special setup before an event or activity.

Since I hate using Windows for stuff, this should all also work on a POSIX
machine (which I use for dev and testing).

## Goal

The intent here is to glue together a bunch of applications for logging and
rig control for a few different use-cases, and to be able to do this for
mutliple rigs on a single machine. These are the constituents:

- [FLRig](https://github.com/w1hkj/flrig) for rig control, connects directly
  to the actual rigs via serial, exposes XMLRPC for sharing
- [WSJT-X](https://wsjt.sourceforge.io/wsjtx.html) for digital modes, mostly
  contests. Connects to FLRig for rig control, with logging and application
  control via UDP
- [JTDX](https://sourceforge.net/projects/jtdx/) for digital modes, mostly
  general DXing. Same as WSJT-X
- [GridTracker](https://gridtracker.org/) for monitoring, control of
  JTDX/WSJT-X, live LoTW uploading, talk to them via UDP
- [JTSync](https://www.dxshell.com/jtsync.html) for dT monitoring and control,
  listens via UDP
- [Log4OM](https://www.log4om.com/) for logging everything. Connects to FLRig
  for rig control (via hamlib), listens on UDP for log events from JTDX/WSJT-X
  *and* separately to N1MM for contest log events
- [N1MM](https://n1mmwp.hamdocs.com/) for logging an individual contest.
  Connects to `rigctlcom` proxy over serial loopback pair for rig control
  (monitoring) and listens to UDP log events from JTDX/WSJT-X

With everything glued together, this is what I get:

- Ability to run multiple rigs and bands at the same time, voice or digital
- All heard stations in any WSJT-X/JTDX instance get received by GridTracker
- Logging a contact via any WSJT-X/JTDX instance goes to GridTracker, Log4OM,
  and N1MM if it is running
- Targeting a station in any WSJT-X/JTDX instance causes Log4OM to look up
  that contact in the log for previous QSOs, bio info, etc
- Clicking on any station in GridTracker causes the appropriate WSJT-X/JTDX
  instance to target that station
- If running a contest, N1MM receives digital contacts logged, but also has
  control of one or more radios for logging voice contacts on the proper
  frequency. Voice contacts are logged by Log4OM as well.

## Glue required

Most of the above components can talk to FLRig, which handles the task of
everything knowing the state of the radio. The exception there is N1MM, which
cannot. That requires some glue to make work.

While WSJT-X, JTDX, and GridTracker can all coexist with UDP multicast, some
others (Log4OM notably) can only listen unicast, which means either chaining
of proxies (Log4OM and GridTracker can forward to one extra location each)
or another approach is required.

### N1MM glue

Since N1MM can't talk to FLRig, a hack is needed. Luckily
[hamlib](https://hamlib.github.io/)'s `rigctlcom`
emulator can help here. It can talk to FLRig natively, and then simulate a
Kenwood TS-2000 on a fake serial port. However, this is just a command-line
utility that not only requires a command prompt to be open (or a service
helper to run in the background) but it also needs to be started and stopped
with FLRig as it does not do a good job of recovering from FLRig being closed
and re-opened. Thus it is not suitable to just always run in the background.

Thus, the `flrigproxy.py` helper here was born. This reads a config file and
monitors multiple FLRig instances. When an FLRig is detected to be started and
listening, it spawns a `rigctlcom` instance for it and proxies communication
between them. Each of these is set to point to a loopback serial port pair
from [com0com](https://com0com.sourceforge.net/), the other side of which
is what N1MM connects to (thinking it's a TS-2000). If FLRig is closed, the
`rigctlcom` instance is killed, to be restarted when FLRig comes back.

### WSJT-X logging glue

The `qsofwdsvc.py` helper provides general-purpose forwarding of WSJT-X/JTDX
logging and control messages. This can be used to copy them to multiple
receiver applications, as well as send them across the network off the
machine for remote monitoring. I use this so I can remotely control the rig,
but run a local copy of GridTracker for alerts.

Running multiple copies of WSJT-X in a contest (VHF and HF for example) is
possible because the forwarder parses the identities of the various instances
and routes replies to the correct instance when something like GridTracker
tries. Thus, all WSJT-X and JTDX instances can "report" to a single port and
the forwarder sorts things appropriately.

## Setup

Python is required, 3.11 has been tested. Dependencies should be installed
as administrator (so they're available in service context):
```
> pip install -r requirements.txt
```

Obviously [com0com0](https://com0com.sourceforge.net/) is required for the
serial port pairs, if using the rig control proxy.

Each service can be registered with the service manager:
```
> python qsofwdsvc.py install
> python flrigproxy.py install
```
and then started:
```
> python qsofwdsvc.py start
> python flrigproxy.py start
```
These should be set to "Automatic" startup in Computer Management, Services
so that they run at boot and are always available.

"""Program used to start a Celery worker instance."""

import os
import sys

import click
from click import ParamType
from click.types import StringParamType

from celery import concurrency
from celery.bin.base import (COMMA_SEPARATED_LIST, LOG_LEVEL,
                             CeleryDaemonCommand, CeleryOption)
from celery.platforms import EX_FAILURE, detached, maybe_drop_privileges
from celery.utils.log import get_logger
from celery.utils.nodenames import default_nodename, host_format, node_format

logger = get_logger(__name__)


def maybe_patch_concurrency(library):
    """Patches gevent/eventlet libraries."""

    def patch_eventlet():
        import eventlet.debug

        eventlet.monkey_patch()
        blockdetect = float(os.environ.get('EVENTLET_NOBLOCK', 0))
        if blockdetect:
            eventlet.debug.hub_blocking_detection(blockdetect, blockdetect)

    def patch_gevent():
        import gevent.monkey
        import gevent.signal

        gevent.monkey.patch_all()

    patches = {
        'eventlet': patch_eventlet,
        'gevent': patch_gevent
    }

    patcher = patches.get(library)
    if patcher:
        patcher()


class CeleryBeat(ParamType):
    """Celery Beat flag."""

    name = "beat"

    def convert(self, value, param, ctx):
        if ctx.obj.app.IS_WINDOWS and value:
            self.fail('-B option does not work on Windows.  '
                      'Please run celery beat as a separate service.')

        return value


class WorkersPool(click.Choice):
    """Workers pool option."""

    name = "pool"

    def __init__(self):
        """Initialize the workers pool option with the relevant choices."""
        super().__init__(('prefork', 'eventlet', 'gevent', 'solo'))

    def convert(self, value, param, ctx):
        # Pools like eventlet/gevent needs to patch libs as early
        # as possible.
        maybe_patch_concurrency(value)

        return concurrency.get_implementation(
            value) or ctx.obj.app.conf.worker_pool


class Hostname(StringParamType):
    """Hostname option."""

    name = "hostname"

    def convert(self, value, param, ctx):
        return host_format(default_nodename(value))


class Autoscale(ParamType):
    """Autoscaling parameter."""

    name = "<min workers>, <max workers>"

    def convert(self, value, param, ctx):
        value = value.split(',')

        if len(value) > 2:
            self.fail("Expected two comma separated integers or one integer."
                      f"Got {len(value)} instead.")

        if len(value) == 1:
            try:
                value = (int(value[0]), 0)
            except ValueError:
                self.fail(f"Expected an integer. Got {value} instead.")

        try:
            return tuple(reversed(sorted(map(int, value))))
        except ValueError:
            self.fail("Expected two comma separated integers."
                      f"Got {value.join(',')} instead.")


CELERY_BEAT = CeleryBeat()
WORKERS_POOL = WorkersPool()
HOSTNAME = Hostname()
AUTOSCALE = Autoscale()

C_FAKEFORK = os.environ.get('C_FAKEFORK')


def detach(path, argv, logfile=None, pidfile=None, uid=None,
           gid=None, umask=None, workdir=None, fake=False, app=None,
           executable=None, hostname=None):
    """Detach program by argv."""
    fake = 1 if C_FAKEFORK else fake
    with detached(logfile, pidfile, uid, gid, umask, workdir, fake,
                  after_forkers=False):
        try:
            if executable is not None:
                path = executable
            os.execv(path, [path] + argv)
        except Exception:  # pylint: disable=broad-except
            if app is None:
                from celery import current_app
                app = current_app
            app.log.setup_logging_subsystem(
                'ERROR', logfile, hostname=hostname)
            logger.critical("Can't exec %r", ' '.join([path] + argv),
                            exc_info=True)
        return EX_FAILURE


@click.command(cls=CeleryDaemonCommand,
               context_settings={
                   'allow_extra_args': True,
                   'ignore_unknown_options': True
               })
@click.option('-n',
              '--hostname',
              default=host_format(default_nodename(None)),
              cls=CeleryOption,
              type=HOSTNAME,
              help_group="Worker Options",
              help="Set custom hostname (e.g., 'w1@%%h').  "
                   "Expands: %%h (hostname), %%n (name) and %%d, (domain).")
@click.option('-D',
              '--detach',
              cls=CeleryOption,
              is_flag=True,
              default=False,
              help_group="Worker Options",
              help="Start worker as a background process.")
@click.option('-S',
              '--statedb',
              cls=CeleryOption,
              type=click.Path(),
              callback=lambda ctx, _, value: value or ctx.obj.app.conf.worker_state_db,
              help_group="Worker Options",
              help="Path to the state database. The extension '.db' may be"
                   "appended to the filename.")
@click.option('-l',
              '--loglevel',
              default='WARNING',
              cls=CeleryOption,
              type=LOG_LEVEL,
              help_group="Worker Options",
              help="Logging level.")
@click.option('optimization',
              '-O',
              default='default',
              cls=CeleryOption,
              type=click.Choice(('default', 'fair')),
              help_group="Worker Options",
              help="Apply optimization profile.")
@click.option('--prefetch-multiplier',
              type=int,
              metavar="<prefetch multiplier>",
              callback=lambda ctx, _, value: value or ctx.obj.app.conf.worker_prefetch_multiplier,
              cls=CeleryOption,
              help_group="Worker Options",
              help="Set custom prefetch multiplier value"
                   "for this worker instance.")
@click.option('-c',
              '--concurrency',
              type=int,
              metavar="<concurrency>",
              callback=lambda ctx, _, value: value or ctx.obj.app.conf.worker_concurrency,
              cls=CeleryOption,
              help_group="Pool Options",
              help="Number of child processes processing the queue.  "
                   "The default is the number of CPUs available"
                   "on your system.")
@click.option('-P',
              '--pool',
              default='prefork',
              type=WORKERS_POOL,
              cls=CeleryOption,
              help_group="Pool Options",
              help="Pool implementation.",
              is_eager=True)
@click.option('-E',
              '--task-events',
              '--events',
              is_flag=True,
              cls=CeleryOption,
              help_group="Pool Options",
              help="Send task-related events that can be captured by monitors"
                   " like celery events, celerymon, and others.")
@click.option('--time-limit',
              type=float,
              cls=CeleryOption,
              help_group="Pool Options",
              help="Enables a hard time limit "
                   "(in seconds int/float) for tasks.")
@click.option('--soft-time-limit',
              type=float,
              cls=CeleryOption,
              help_group="Pool Options",
              help="Enables a soft time limit "
                   "(in seconds int/float) for tasks.")
@click.option('--max-tasks-per-child',
              type=int,
              cls=CeleryOption,
              help_group="Pool Options",
              help="Maximum number of tasks a pool worker can execute before "
                   "it's terminated and replaced by a new worker.")
@click.option('--max-memory-per-child',
              type=int,
              cls=CeleryOption,
              help_group="Pool Options",
              help="Maximum amount of resident memory, in KiB, that may be "
                   "consumed by a child process before it will be replaced "
                   "by a new one.  If a single task causes a child process "
                   "to exceed this limit, the task will be completed and "
                   "the child process will be replaced afterwards.\n"
                   "Default: no limit.")
@click.option('--purge',
              '--discard',
              is_flag=True,
              cls=CeleryOption,
              help_group="Queue Options")
@click.option('--queues',
              '-Q',
              type=COMMA_SEPARATED_LIST,
              cls=CeleryOption,
              help_group="Queue Options")
@click.option('--exclude-queues',
              '-X',
              type=COMMA_SEPARATED_LIST,
              cls=CeleryOption,
              help_group="Queue Options")
@click.option('--include',
              '-I',
              type=COMMA_SEPARATED_LIST,
              cls=CeleryOption,
              help_group="Queue Options")
@click.option('--without-gossip',
              default=False,
              cls=CeleryOption,
              help_group="Features")
@click.option('--without-mingle',
              default=False,
              cls=CeleryOption,
              help_group="Features")
@click.option('--without-heartbeat',
              default=False,
              cls=CeleryOption,
              help_group="Features", )
@click.option('--heartbeat-interval',
              type=int,
              cls=CeleryOption,
              help_group="Features", )
@click.option('--autoscale',
              type=AUTOSCALE,
              cls=CeleryOption,
              help_group="Features", )
@click.option('-B',
              '--beat',
              type=CELERY_BEAT,
              cls=CeleryOption,
              is_flag=True,
              help_group="Embedded Beat Options")
@click.option('-s',
              '--schedule-filename',
              '--schedule',
              callback=lambda ctx, _, value: value or ctx.obj.app.conf.beat_schedule_filename,
              cls=CeleryOption,
              help_group="Embedded Beat Options")
@click.option('--scheduler',
              cls=CeleryOption,
              help_group="Embedded Beat Options")
@click.argument('user_extra_params', nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def worker(ctx, hostname=None, pool_cls=None, uid=None, gid=None,
           loglevel=None, logfile=None, pidfile=None, statedb=None,
           user_extra_params=None,
           **kwargs):
    """Start worker instance.

    \b
    Examples
    --------
    $ celery worker --app=proj -l info
    $ celery worker -A proj -l info -Q hipri,lopri
    $ celery worker -A proj --concurrency=4
    $ celery worker -A proj --concurrency=1000 -P eventlet
    $ celery worker --autoscale=10,0

    """
    app = ctx.obj.app

    user_opts = app.user_options.get('worker')
    if user_opts:
        user_options = {}

        def cb(_, param, val):
            user_options[param.name] = val
            return val

        for x in user_opts:
            x.callback = cb

        cmd = click.Command("user", params=list(user_opts))
        cmd.parse_args(ctx, list(user_extra_params))

        kwargs.update(user_options)

    if ctx.args:
        try:
            app.config_from_cmdline(ctx.args, namespace='worker')
        except (KeyError, ValueError) as e:
            # TODO: Improve the error messages
            raise click.UsageError(
                "Unable to parse extra configuration from command line.\n"
                f"Reason: {e}", ctx=ctx)
    if kwargs.get('detach', False):
        params = ctx.params.copy()
        params.pop('detach')
        params.pop('logfile')
        params.pop('pidfile')
        params.pop('uid')
        params.pop('gid')
        umask = params.pop('umask')
        workdir = ctx.obj.workdir
        params.pop('hostname')
        executable = params.pop('executable')
        argv = ['-m', 'celery', 'worker']
        for arg, value in params.items():
            if isinstance(value, bool) and value:
                argv.append(f'--{arg}')
            else:
                if value is not None:
                    argv.append(f'--{arg}')
                    argv.append(str(value))
            return detach(sys.executable,
                          argv,
                          logfile=logfile,
                          pidfile=pidfile,
                          uid=uid, gid=gid,
                          umask=umask,
                          workdir=workdir,
                          app=app,
                          executable=executable,
                          hostname=hostname)
        return
    maybe_drop_privileges(uid=uid, gid=gid)
    worker = app.Worker(
        hostname=hostname, pool_cls=pool_cls, loglevel=loglevel,
        logfile=logfile,  # node format handled by celery.app.log.setup
        pidfile=node_format(pidfile, hostname),
        statedb=node_format(statedb, hostname),
        no_color=ctx.obj.no_color,
        **kwargs)
    worker.start()
    return worker.exitcode

"""CLI application for discovering and controlling Apple TVs."""

import sys
import inspect
import logging
import binascii
import asyncio

import argparse
from argparse import ArgumentTypeError
from zeroconf import Zeroconf

import pyatv
import pyatv.pairing
from pyatv import (const, dmap, exceptions, tag_definitions)
from pyatv.interface import retrieve_commands


class PushListener:
    """Internal listener for push updates."""

    @staticmethod
    def playstatus_update(_, playstatus):
        """Print what is currently playing when it changes."""
        print(str(playstatus))
        print(20*'-')

    @staticmethod
    def playstatus_error(updater, exception):
        """Inform about an error and restart push updates."""
        print("An error occurred (restarting): {0}".format(exception))
        updater.start(initial_delay=1)


def _in_range(lower, upper):
    def _checker(value):
        if int(value) >= lower and int(value) < upper:
            return int(value)
        raise ArgumentTypeError('Must be greater >= {} and < {}'.format(
            lower, upper))
    return _checker


@asyncio.coroutine
def cli_handler(loop):
    """Application starts here."""
    parser = argparse.ArgumentParser()

    parser.add_argument('command', nargs='+')
    parser.add_argument('--name', help='apple tv name',
                        dest='name', default='Apple TV')
    parser.add_argument('--address', help='device ip address or hostname',
                        dest='address', default=None)
    parser.add_argument('-t', '--scan-timeout', help='timeout when scanning',
                        dest='scan_timeout', type=_in_range(1, 10),
                        metavar='TIMEOUT', default=3)
    parser.add_argument('--version', action='version',
                        help='version of atvremote and pyatv',
                        version='%(prog)s {0}'.format(const.__version__))

    pairing = parser.add_argument_group('pairing')
    pairing.add_argument('--remote-name', help='remote pairing name',
                         dest='remote_name', default='pyatv')
    pairing.add_argument('-p', '--pin', help='pairing pin code',
                         dest='pin_code', type=_in_range(0, 9999),
                         metavar='PIN', default=1234)
    pairing.add_argument('--pairing-guid',
                         help='pairing guid (16 chars hex)',
                         dest='pairing_guid', default=None)

    ident = parser.add_mutually_exclusive_group()
    ident.add_argument('-a', '--autodiscover',
                       help='automatically find a device',
                       action='store_true', dest='autodiscover', default=False)
    ident.add_argument('--login_id', help='home sharing id or pairing guid',
                       dest='login_id', default=None)

    debug = parser.add_argument_group('debugging')
    debug.add_argument('-v', '--verbose', help='increase output verbosity',
                       action='store_true', dest='verbose')
    debug.add_argument('--developer', help='show developer commands',
                       action='store_true', dest='developer',
                       default=False)
    debug.add_argument('--debug', help='print debug information',
                       action='store_true', dest='debug')

    args = parser.parse_args()
    loglevel = logging.WARNING
    if args.verbose:
        loglevel = logging.INFO
    if args.debug:
        loglevel = logging.DEBUG

    logging.basicConfig(level=loglevel,
                        format='%(levelname)s: %(message)s')
    logging.getLogger('requests').setLevel(logging.WARNING)

    # Sanity checks that not can be done natively by argparse
    if (args.login_id and not args.address) or \
            (not args.login_id and args.address):
        parser.error('both --login_id and --address must be given')

    if args.command[0] == 'scan':
        return (yield from _handle_scan(args, loop))
    elif args.command[0] == 'pair':
        return (yield from _handle_pairing(args, loop))
    elif args.autodiscover:
        return (yield from _handle_autodiscover(args, loop))
    elif args.login_id:
        return (yield from _handle_commands(args, loop))
    else:
        logging.error('To autodiscover an Apple TV, add -a')

    return 1


@asyncio.coroutine
def _handle_scan(args, loop):
    atvs = yield from pyatv.scan_for_apple_tvs(
        loop, timeout=args.scan_timeout, only_home_sharing=False)
    _print_found_apple_tvs(atvs)

    return 0


def _print_found_apple_tvs(atvs, outstream=sys.stdout):
    print('Found Apple TVs:', file=outstream)
    for apple_tv in atvs:
        if apple_tv.login_id is None:
            msg = ' - {0} at {1} (home sharing disabled)'.format(
                apple_tv.name, apple_tv.address)
        else:
            msg = ' - {0} at {1} (login id: {2})'.format(
                apple_tv.name, apple_tv.address, apple_tv.login_id)
        print(msg, file=outstream)

    print("\nNote: You must use 'pair' with devices "
          "that have home sharing disabled", file=outstream)


@asyncio.coroutine
def _handle_pairing(args, loop):
    handler = pyatv.pair_with_apple_tv(
        loop, args.pin_code, args.remote_name, pairing_guid=args.pairing_guid)
    print('Use pin {} to pair with "{}" (press ENTER to stop)'.format(
        args.pin_code, args.remote_name))
    print('Using pairing guid: 0x' + handler.pairing_guid)
    print('Note: If remote does not show up, try rebooting your Apple TV')

    yield from handler.start(Zeroconf())
    yield from loop.run_in_executor(None, sys.stdin.readline)
    yield from handler.stop()

    # Give some feedback to the user
    if handler.has_paired:
        print('Pairing seems to have succeeded, yey!')
        print('You may now use this login id: 0x{}'.format(
            handler.pairing_guid))
    else:
        print('No response from Apple TV!')

    return 0


@asyncio.coroutine
def _handle_autodiscover(args, loop):
    atvs = yield from pyatv.scan_for_apple_tvs(
        loop, timeout=args.scan_timeout, abort_on_found=True)
    if not atvs:
        logging.error('Could not find any Apple TV on current network')
        return 1
    elif len(atvs) > 1:
        logging.error('Found more than one Apple TV; '
                      'specify one using --address and --login_id')
        _print_found_apple_tvs(atvs, outstream=sys.stderr)
        return 1

    # Simple hack to re-use existing command handling and respect options
    apple_tv = atvs[0]
    args.address = apple_tv.address
    args.login_id = apple_tv.login_id
    args.name = apple_tv.name
    logging.info('Auto-discovered %s at %s', args.name, args.address)

    return (yield from _handle_commands(args, loop))


def _print_commands(title, obj, newline=True):
    commands = ' - ' + '\n - '.join(
        map(lambda x: x[0] + ' - ' + x[1], sorted(obj.items())))
    print('{} commands:\n{}{}'.format(
        title, commands, '\n' if newline else ''))


def _extract_command_with_args(cmd):
    """Parse input command with arguments.

    Parses the input command in such a way that the user may
    provide additional argument to the command. The format used is this:
      command=arg1,arg2,arg3,...
    all the additional arguments are passed as arguments to the target
    method.
    """
    equal_sign = cmd.find('=')
    if equal_sign == -1:
        return cmd, []

    command = cmd[0:equal_sign]
    args = cmd[equal_sign+1:].split(',')
    return command, args


@asyncio.coroutine
def _handle_commands(args, loop):
    details = pyatv.AppleTVDevice(args.name, args.address, args.login_id)
    atv = pyatv.connect_to_apple_tv(details, loop)
    atv.push_updater.listener = PushListener()

    try:
        for cmd in args.command:
            ret = yield from _handle_command(args, cmd, atv, loop)
            if ret != 0:
                return ret
    finally:
        yield from atv.logout()

    return 0


# pylint: disable=too-many-return-statements
@asyncio.coroutine
def _handle_command(args, cmd, atv, loop):
    # TODO: Add these to array and use a loop
    playing_resp = yield from atv.metadata.playing()
    ctrl = retrieve_commands(atv.remote_control, developer=args.developer)
    metadata = retrieve_commands(atv.metadata, developer=args.developer)
    playing = retrieve_commands(playing_resp, developer=args.developer)
    airplay = retrieve_commands(atv.airplay, developer=args.developer)
    other = {'push_updates': 'Listen for push updates'}

    # Parse input command and argument from user
    cmd, cmd_args = _extract_command_with_args(cmd)
    if cmd == 'commands':
        _print_commands('Remote control', ctrl)
        _print_commands('Metadata', metadata)
        _print_commands('Playing', playing)
        _print_commands('AirPlay', airplay)
        _print_commands('Other', other, newline=False)

    elif cmd == 'artwork':
        artwork = yield from atv.metadata.artwork()
        if artwork is not None:
            with open('artwork.png', 'wb') as file:
                file.write(artwork)
        else:
            print('No artwork is currently available.')
            return 1

    elif cmd == 'push_updates':
        print('Press ENTER to stop')

        atv.push_updater.start()
        yield from loop.run_in_executor(None, sys.stdin.readline)
        atv.push_updater.stop()

    elif cmd in ctrl:
        return (yield from _exec_command(atv.remote_control, cmd, *cmd_args))

    elif cmd in metadata:
        return (yield from _exec_command(atv.metadata, cmd, *cmd_args))

    elif cmd in playing:
        return (yield from _exec_command(playing_resp, cmd, *cmd_args))

    elif cmd in airplay:
        return (yield from _exec_command(atv.airplay, cmd, *cmd_args))

    else:
        logging.error('Unknown command: %s', args.command[0])
        return 1

    return 0


@asyncio.coroutine
def _exec_command(obj, command, *args):
    try:
        # If the command to execute is a @property, the value returned by that
        # property will be stored in tmp. Otherwise it's a coroutine and we
        # have to yield for the result and wait until it is available.
        tmp = getattr(obj, command)
        if inspect.ismethod(tmp):
            value = yield from tmp(*args)
        else:
            value = tmp
        _pretty_print(value)
        return 0
    except NotImplementedError:
        logging.fatal("Command '%s' is not supported by device", command)
    except exceptions.AuthenticationError as ex:
        logging.fatal('Authentication error: %s', str(ex))
    return 1


def _pretty_print(data):
    if data is None:
        return
    if isinstance(data, bytes):
        print(binascii.hexlify(data))
    elif isinstance(data, list):
        print(dmap.pprint(data, tag_definitions.lookup_tag))
    else:
        print(data)


def main():
    """Start the asyncio event loop and runs the application."""
    # Helper method so that the coroutine exits cleanly if an exception
    # happens (which would leave resources dangling)
    @asyncio.coroutine
    def _run_application(loop):
        try:
            return (yield from cli_handler(loop))

        except KeyboardInterrupt:
            pass  # User pressed Ctrl+C, just ignore it

        except SystemExit:
            pass  # sys.exit() was used - do nothing

        except:  # pylint: disable=bare-except
            import traceback

            traceback.print_exc(file=sys.stderr)
            sys.stderr.writelines(
                '\n>>> An error occurred, full stack trace above\n')

        return 1

    try:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(_run_application(loop))
    except KeyboardInterrupt:
        pass

    return 1


if __name__ == '__main__':
    sys.exit(main())

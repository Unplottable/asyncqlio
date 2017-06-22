"""
AsyncQLio migration tool.

This tool contains utilities to perform migrations on database schemas.
"""
import asyncio
import functools
import logging
import os
import re
import sys
import typing
from pathlib import Path

import click
import tqdm

from asyncqlio.db import DatabaseInterface
from asyncqlio.orm.session import Session


# copied from tqdm pypi page
# fuck up stdout
class DummyTqdmFile(object):
    """
    Dummy file-like that will write to tqdm
    """

    def __init__(self, file):
        self.file = file

    # needed for colour output
    def isatty(self) -> bool:
        try:
            return self.file.isatty()
        except AttributeError:
            return False

    # main write method
    def write(self, x: typing.Union[bytes, str]) -> None:
        # fuck you, click, I know what I'm doing
        if isinstance(x, bytes):
            data = x.decode()
        else:
            data = x
        if len(data.rstrip()) > 0:
            # make data look less stupid
            if data[-1] == "\n":
                end = ""
            else:
                end = "\n"
            tqdm.tqdm.write(data, file=self.file, end=end)

    # this is almost definitely called by click at some point
    def flush(self) -> None:
        self.file.flush()


sys.stdout = DummyTqdmFile(sys.__stdout__)


# code copied from https://stackoverflow.com/a/38739634 by
class TqdmLoggingHandler(logging.Handler):
    def __init__(self, level=logging.NOTSET):
        super(self.__class__, self).__init__(level)

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.tqdm.write(msg)
            self.flush()
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.handleError(record)


# redirect logging output through tqdm
handler = TqdmLoggingHandler(level=logging.DEBUG)
# todo: make this configurable, i guess
formatter = logging.Formatter('[%(levelname)s] %(name)s -> %(message)s')
handler.setFormatter(formatter)
logging.basicConfig(handlers=[handler], level=logging.DEBUG)

logger = logging.getLogger("asyncqlio.migrations")

# definitions of certain template files
env_file = '''"""
Example environment file for asql-migrate.
"""
import typing

from asyncqlio.db import DatabaseInterface
from asyncqlio.orm.session import Session

# If you need to import your own tables, do so here.
# import sys, os
# sys.path.insert(0, os.path.abspath("."))
# import my_package.Table

# The DSN to connect to the server with.
# You probably want to change this. 
dsn = {dsn}

async def create_database_interface() -> DatabaseInterface:
    """
    Creates the database interface used by the migrations.
    """
    # If you wish to override how the database interface is created, do so here.
    # This includes importing your Table object, and binding tables.
    if dsn is None:
        raise RuntimeError("No DSN provided! Either edit it in env.py, or provide it on the " 
                           "command line.")
    
    db = DatabaseInterface(dsn=dsn)
    await db.connect()
    return db
    

sig = typing.Callable[[Session], None]


async def run_migration_online(db: Session, upgrade: sig):
    """
    Runs a migration file "online". This will acquire a session, call the upgrade function, 
    and then commit the session.
    """
    await upgrade(sess)

        
async def run_migration_offline(db: DatabaseInterface, upgrade: sig):
    """
    Runs a migration file "offline". 
    """
'''

# globals and utility functions
migrations_dir = Path("migrations")


# lol hackiest cache wrapper ever
@functools.lru_cache()
def eval_env() -> dict:
    """
    Evals the env.py script.
    """
    p = migrations_dir / "env.py"
    # dict for the locals
    # this is passed to exec to fill up
    d = {}

    # open the file in text mode and exec() it
    with p.open(mode='r') as f:
        content = f.read()

    exec(content, d, d)
    return d


def get_revision(revision: str, current_revision: int) -> int:
    """
    Gets the absolute revision to upgrade/downgrade to.
    """
    # HEAD means the latest possible revision
    if revision.lower() == "head":
        # return some stupidly high value
        # nobody will ever hit this
        return 999999999999

    # parse relatives
    # + means plus relative to current
    # - means minus relative to current
    if revision.startswith("+"):
        return current_revision + int(revision[1:])

    if revision.startswith("-"):
        return current_revision - int(revision[1:])

    # try and convert it straight to an int
    try:
        return int(revision)
    except ValueError:
        raise RuntimeError("Revision {} could not be parsed as absolute or relative"
                           .format(revision))


async def get_current_version(iface: DatabaseInterface) -> int:
    """
    Gets the current version of the database.

    This will create the asql_version table if it doesn't already exist.
    """
    async with iface.get_session() as sess:  # type: Session
        await sess.execute('''CREATE TABLE IF NOT EXISTS asql_version (version INTEGER)''')
        row = await (await sess.cursor('''SELECT version from asql_version''')).fetch_row()
        if row is None:
            await sess.execute('''INSERT INTO asql_version VALUES (0)''')
        else:
            return row["version"]


def coro(func):
    """
    A decorator that runs a command inside asyncio.
    """

    @functools.wraps(func)
    def coro_wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        fut = asyncio.ensure_future(func(*args, **kwargs), loop=loop)
        result = loop.run_until_complete(fut)
        return result

    return coro_wrapper


@click.group()
@click.option("-m", "--migration-dir", help="The directory migrations will be stored",
              default="migrations")
def cli(migration_dir: str):
    # lol global state
    global migrations_dir
    migrations_dir = Path(migration_dir)


@cli.command()
@click.option("-d", "--dsn", required=False, default=None)
def init(directory: str, dsn: str):
    """
    Initializes a migrations directory.
    """
    try:
        os.makedirs(directory)
    except FileExistsError:
        click.secho("Unable to make directory (it exists)!", fg='red')

    if dsn is not None:
        dsn = '"{}"'.format(dsn)

    click.secho("Writing env.py...", fg='blue')
    (Path(directory) / "env.py").write_text(env_file.format(dsn=dsn))
    click.secho("Making versions directory...", fg='blue')
    (Path(directory) / "versions").mkdir(mode=0o755)
    (Path(directory) / "README").write_text("Basic asql-migrate setup.")
    click.secho("Done!", fg='green')


@cli.command()
@click.argument("revision", default="head")
@coro
async def migrate(revision: str = "head"):
    """
    Upgrades the database to the specified revision.
    """
    # open and eval env.py
    env = eval_env()
    # create the interface
    interface = await env["create_database_interface"]()

    async with interface:
        # get the current revision
        current_revision = await get_current_version(interface)
        if current_revision < 0:
            click.secho("Database is in bad shape: revision {} < 0".format(current_revision))
            return
        # parse the revision to upgrade to
        next_revision = get_revision(revision, current_revision)
        if next_revision == current_revision:
            click.secho("Nothing to do. ", fg='cyan')
            return

        if next_revision < 0:
            click.secho("Cannot downgrade past revision 0."
                        " (attempted to downgrade to {})".format(next_revision), fg='magenta')
            return

        await _do_migrate(interface, current_revision, next_revision)


def _get_files() -> typing.List[Path]:
    """
    Gets the migration files.
    """
    files = [x for x in (migrations_dir / "versions").iterdir()
             if re.match(r"[0-9]+.*\.py", x.name)]
    f = sorted(files, key=lambda path: path.name)
    # ensure there are no gaps
    for n, path in enumerate(f):  # type: typing.Tuple[int, Path]
        number = re.match(r"([0-9]+)", path.name)
        if not number:
            raise RuntimeError("Unable to match {}".format(path.name))

        if int(number.groups()[0]) != n + 1:
            raise RuntimeError("Migration versions are missing entry {}".format(n + 1))

    return f


async def _do_migrate(interface: DatabaseInterface, current_revision: int, revision: int):
    """
    Does a migration.
    """
    try:
        files = _get_files()
    except RuntimeError as e:
        click.secho("Aborting upgrade: {}".format(e.args[0]), fg='magenta')
        return

    if revision > current_revision:
        mode = "upgrade"
        slice = files[current_revision:revision]
    else:
        # calculate the bounds slightly differently
        mode = "downgrade"
        if revision <= 0:
            slice = files[current_revision:None:-1]
        else:
            slice = files[current_revision:revision - 1:-1]

    if not slice:
        click.secho("No migrations found to upgrade to revision {}.".format(revision), fg='magenta')
        return

    executed_revision = current_revision
    for migration in tqdm.tqdm(iterable=slice, desc="Migrating", unit="migrations"):  # type: Path
        # read in the migration file data
        # then exec() the data to get the upgrade/downgrade functions
        data = migration.read_text()
        loc = {}
        exec(data, loc, loc)

        env = eval_env()
        upgrade_func = env["run_migration_online"]
        try:
            migration_func = loc[mode]
        except KeyError:
            click.secho("No function '{}' found in migration file {}!"
                        .format(mode, migration.name), fg='magenta')
            return
        # call upgrade_func to upgrade the migration
        logger.info("Using migration file {}".format(migration.name))
        logger.info("Calling migration function {}".format(migration_func))
        async with interface.get_session() as sess:
            await upgrade_func(sess, migration_func)
            if mode == "upgrade":
                executed_revision += 1
            else:
                executed_revision -= 1
            await sess.execute('''UPDATE asql_version SET version = {val}''',
                               {"val": executed_revision})


if __name__ == '__main__':
    cli()

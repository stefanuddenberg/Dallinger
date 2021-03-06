"""Create a connection to the database."""

import logging
import os
import random
import sys
import time
from contextlib import contextmanager
from functools import wraps

import psycopg2
from psycopg2.extensions import TransactionRollbackError
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from dallinger.utils import connect_to_redis

logger = logging.getLogger("dallinger.db")

db_url_default = "postgresql://dallinger:dallinger@localhost/dallinger"
db_url = os.environ.get("DATABASE_URL", db_url_default)
engine = create_engine(db_url, pool_size=1000)
session_factory = sessionmaker(autocommit=False, autoflush=True, bind=engine)
session = scoped_session(session_factory)

Base = declarative_base()
Base.query = session.query_property()

redis_conn = connect_to_redis()

db_user_warning = """
*********************************************************
*********************************************************


Dallinger now requires a database user named "dallinger".

Run:

    createuser -P dallinger --createdb

Consult the developer guide for more information.


*********************************************************
*********************************************************

"""


def check_connection():
    """Test that postgres is running and that we can connect using the
    configured URI.

    Raises a psycopg2.OperationalError on failure.
    """
    conn = psycopg2.connect(db_url)
    conn.close()


@contextmanager
def sessions_scope(local_session, commit=False):
    """Provide a transactional scope around a series of operations."""
    try:
        yield local_session
        if commit:
            local_session.commit()
            logger.debug("DB session auto-committed as requested")
    except Exception as e:
        # We log the exception before re-raising it, in case the rollback also
        # fails
        logger.exception("Exception during scoped worker transaction, " "rolling back.")
        # This rollback is potentially redundant with the remove call below,
        # depending on how the scoped session is configured, but we'll be
        # explicit here.
        local_session.rollback()
        raise e
    finally:
        local_session.remove()
        logger.debug("Session complete, db session closed")


def scoped_session_decorator(func):
    """Manage contexts and add debugging to db sessions."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        with sessions_scope(session):
            # The session used in func comes from the funcs globals, but
            # it will be a proxied thread local var from the session
            # registry, and will therefore be identical to the one returned
            # by the context manager above.
            logger.debug("Running worker %s in scoped DB session", func.__name__)
            return func(*args, **kwargs)

    return wrapper


def init_db(drop_all=False, bind=engine):
    """Initialize the database, optionally dropping existing tables."""
    try:
        if drop_all:
            Base.metadata.drop_all(bind=bind)
        Base.metadata.create_all(bind=bind)
    except OperationalError as err:
        msg = 'password authentication failed for user "dallinger"'
        if msg in err.message:
            sys.stderr.write(db_user_warning)
        raise

    return session


def serialized(func):
    """Run a function within a db transaction using SERIALIZABLE isolation.

    With this isolation level, committing will fail if this transaction
    read data that was since modified by another transaction. So we need
    to handle that case and retry the transaction.
    """

    @wraps(func)
    def wrapper(*args, **kw):
        attempts = 100
        session.remove()
        while attempts > 0:
            try:
                session.connection(
                    execution_options={"isolation_level": "SERIALIZABLE"}
                )
                result = func(*args, **kw)
                session.commit()
                return result
            except OperationalError as exc:
                session.rollback()
                if isinstance(exc.orig, TransactionRollbackError):
                    if attempts > 0:
                        attempts -= 1
                    else:
                        raise Exception(
                            "Could not commit serialized transaction "
                            "after 100 attempts."
                        )
                else:
                    raise
            finally:
                session.remove()
            time.sleep(random.expovariate(0.5))

    return wrapper


# Reset outbox when session begins
@event.listens_for(Session, "after_begin")
def after_begin(session, transaction, connection):
    session.info["outbox"] = []


# Reset outbox after rollback
@event.listens_for(Session, "after_soft_rollback")
def after_soft_rollback(session, previous_transaction):
    session.info["outbox"] = []


def queue_message(channel, message):
    logger.debug("Enqueueing message to {}: {}".format(channel, message))
    if "outbox" not in session.info:
        session.info["outbox"] = []
    session.info["outbox"].append((channel, message))


# Publish messages to redis after commit
@event.listens_for(Session, "after_commit")
def after_commit(session):

    for channel, message in session.info.get("outbox", ()):
        logger.debug("Publishing message to {}: {}".format(channel, message))
        redis_conn.publish(channel, message)

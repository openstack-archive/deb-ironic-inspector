#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


"""
Tests for database migrations. There are "opportunistic" tests here, supported
backends are: sqlite (used in test environment by default), mysql and
postgresql, which are required properly configured unit test environment.

For the opportunistic testing you need to set up a db named 'openstack_citest'
with user 'openstack_citest' and password 'openstack_citest' on localhost.
The test will then use that db and u/p combo to run the tests.

"""


import contextlib

import alembic
from alembic import script
import mock
from oslo_config import cfg
from oslo_db.sqlalchemy.migration_cli import ext_alembic
from oslo_db.sqlalchemy import test_base
from oslo_db.sqlalchemy import test_migrations
from oslo_db.sqlalchemy import utils as db_utils
from oslo_log import log as logging
import sqlalchemy

from ironic_inspector.common.i18n import _LE
from ironic_inspector import db
from ironic_inspector import dbsync
from ironic_inspector.test import base

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


def _get_connect_string(backend, user, passwd, database):
    """Get database connection

    Try to get a connection with a very specific set of values, if we get
    these then we'll run the tests, otherwise they are skipped
    """
    if backend == "sqlite":
        backend = "sqlite"
    elif backend == "postgres":
        backend = "postgresql+psycopg2"
    elif backend == "mysql":
        backend = "mysql+mysqldb"
    else:
        raise Exception("Unrecognized backend: '%s'" % backend)

    return ("%(backend)s://%(user)s:%(passwd)s@localhost/%(database)s"
            % {'backend': backend, 'user': user, 'passwd': passwd,
               'database': database})


def _is_backend_avail(backend, user, passwd, database):
    try:
        connect_uri = _get_connect_string(backend, user, passwd, database)
        engine = sqlalchemy.create_engine(connect_uri)
        connection = engine.connect()
    except Exception:
        # intentionally catch all to handle exceptions even if we don't
        # have any backend code loaded.
        return False
    else:
        connection.close()
        engine.dispose()
        return True


class FakeFacade(object):
    def __init__(self, engine):
        self.engine = engine

    def get_engine(self):
        return self.engine


@contextlib.contextmanager
def patch_with_engine(engine):
    with mock.patch.object(db, 'create_facade_lazily') as patch_engine:
        patch_engine.return_value = FakeFacade(engine)
        yield


class WalkVersionsMixin(object):
    def _walk_versions(self, engine=None, alembic_cfg=None):
        # Determine latest version script from the repo, then
        # upgrade from 1 through to the latest, with no data
        # in the databases. This just checks that the schema itself
        # upgrades successfully.

        with patch_with_engine(engine):
            script_directory = script.ScriptDirectory.from_config(alembic_cfg)

            self.assertIsNone(self.migration_ext.version())

            versions = [ver for ver in script_directory.walk_revisions()]

            for version in reversed(versions):
                self._migrate_up(engine, alembic_cfg,
                                 version.revision, with_data=True)

    def _migrate_up(self, engine, config, version, with_data=False):
        """migrate up to a new version of the db.

        We allow for data insertion and post checks at every
        migration version with special _pre_upgrade_### and
        _check_### functions in the main test.
        """
        # NOTE(sdague): try block is here because it's impossible to debug
        # where a failed data migration happens otherwise
        try:
            if with_data:
                data = None
                pre_upgrade = getattr(
                    self, "_pre_upgrade_%s" % version, None)
                if pre_upgrade:
                    data = pre_upgrade(engine)

            self.migration_ext.upgrade(version)
            self.assertEqual(version, self.migration_ext.version())
            if with_data:
                check = getattr(self, "_check_%s" % version, None)
                if check:
                    check(engine, data)
        except Exception:
            LOG.error(_LE("Failed to migrate to version %(version)s on engine "
                          "%(engine)s"),
                      {'version': version, 'engine': engine})
            raise


class TestWalkVersions(base.BaseTest, WalkVersionsMixin):
    def setUp(self):
        super(TestWalkVersions, self).setUp()
        self.engine = mock.MagicMock()
        self.migration_ext = mock.MagicMock()
        self.config = mock.MagicMock()
        self.versions = [mock.Mock(revision='2b2'), mock.Mock(revision='1a1')]

    def test_migrate_up(self):
        self.migration_ext.version.return_value = 'dsa123'

        self._migrate_up(self.engine, self.config, 'dsa123')

        self.migration_ext.version.assert_called_with()

    def test_migrate_up_with_data(self):
        test_value = {"a": 1, "b": 2}
        self.migration_ext.version.return_value = '141'
        self._pre_upgrade_141 = mock.MagicMock()
        self._pre_upgrade_141.return_value = test_value
        self._check_141 = mock.MagicMock()

        self._migrate_up(self.engine, self.config, '141', True)

        self._pre_upgrade_141.assert_called_with(self.engine)
        self._check_141.assert_called_with(self.engine, test_value)

    @mock.patch.object(script, 'ScriptDirectory')
    @mock.patch.object(WalkVersionsMixin, '_migrate_up')
    def test_walk_versions_all_default(self, _migrate_up, script_directory):
        fc = script_directory.from_config()
        fc.walk_revisions.return_value = self.versions
        self.migration_ext.version.return_value = None

        self._walk_versions(self.engine, self.config)

        self.migration_ext.version.assert_called_with()

        upgraded = [mock.call(self.engine, self.config, v.revision,
                    with_data=True) for v in reversed(self.versions)]
        self.assertEqual(self._migrate_up.call_args_list, upgraded)

    @mock.patch.object(script, 'ScriptDirectory')
    @mock.patch.object(WalkVersionsMixin, '_migrate_up')
    def test_walk_versions_all_false(self, _migrate_up, script_directory):
        fc = script_directory.from_config()
        fc.walk_revisions.return_value = self.versions
        self.migration_ext.version.return_value = None

        self._walk_versions(self.engine, self.config)

        upgraded = [mock.call(self.engine, self.config, v.revision,
                    with_data=True) for v in reversed(self.versions)]
        self.assertEqual(upgraded, self._migrate_up.call_args_list)


class MigrationCheckersMixin(object):
    def setUp(self):
        super(MigrationCheckersMixin, self).setUp()
        self.config = dbsync._get_alembic_config()
        self.config.ironic_inspector_config = CONF
        # create AlembicExtension with fake config and replace
        # with real one.
        self.migration_ext = ext_alembic.AlembicExtension(
            self.engine, {'alembic_ini_path': ''})
        self.migration_ext.config = self.config

    def test_walk_versions(self):
        self._walk_versions(self.engine, self.config)

    def test_connect_fail(self):
        """Test that we can trigger a database connection failure

        Test that we can fail gracefully to ensure we don't break people
        without specific database backend
        """
        if _is_backend_avail(self.FIXTURE.DRIVER, "openstack_cifail",
                             self.FIXTURE.USERNAME, self.FIXTURE.DBNAME):
            self.fail("Shouldn't have connected")

    def _check_578f84f38d(self, engine, data):
        nodes = db_utils.get_table(engine, 'nodes')
        col_names = [column.name for column in nodes.c]
        self.assertIn('uuid', col_names)
        self.assertIsInstance(nodes.c.uuid.type, sqlalchemy.types.String)
        self.assertIn('started_at', col_names)
        self.assertIsInstance(nodes.c.started_at.type, sqlalchemy.types.Float)
        self.assertIn('finished_at', col_names)
        self.assertIsInstance(nodes.c.started_at.type, sqlalchemy.types.Float)
        self.assertIn('error', col_names)
        self.assertIsInstance(nodes.c.error.type, sqlalchemy.types.Text)

        attributes = db_utils.get_table(engine, 'attributes')
        col_names = [column.name for column in attributes.c]
        self.assertIn('uuid', col_names)
        self.assertIsInstance(attributes.c.uuid.type, sqlalchemy.types.String)
        self.assertIn('name', col_names)
        self.assertIsInstance(attributes.c.name.type, sqlalchemy.types.String)
        self.assertIn('value', col_names)
        self.assertIsInstance(attributes.c.value.type, sqlalchemy.types.String)

        options = db_utils.get_table(engine, 'options')
        col_names = [column.name for column in options.c]
        self.assertIn('uuid', col_names)
        self.assertIsInstance(options.c.uuid.type, sqlalchemy.types.String)
        self.assertIn('name', col_names)
        self.assertIsInstance(options.c.name.type, sqlalchemy.types.String)
        self.assertIn('value', col_names)
        self.assertIsInstance(options.c.value.type, sqlalchemy.types.Text)

    def _check_d588418040d(self, engine, data):
        rules = db_utils.get_table(engine, 'rules')
        col_names = [column.name for column in rules.c]
        self.assertIn('uuid', col_names)
        self.assertIsInstance(rules.c.uuid.type, sqlalchemy.types.String)
        self.assertIn('created_at', col_names)
        self.assertIsInstance(rules.c.created_at.type,
                              sqlalchemy.types.DateTime)
        self.assertIn('description', col_names)
        self.assertIsInstance(rules.c.description.type, sqlalchemy.types.Text)
        self.assertIn('disabled', col_names)
        # in some backends bool type is integer
        self.assertTrue(isinstance(rules.c.disabled.type,
                                   sqlalchemy.types.Boolean) or
                        isinstance(rules.c.disabled.type,
                                   sqlalchemy.types.Integer))

        conditions = db_utils.get_table(engine, 'rule_conditions')
        col_names = [column.name for column in conditions.c]
        self.assertIn('id', col_names)
        self.assertIsInstance(conditions.c.id.type, sqlalchemy.types.Integer)
        self.assertIn('rule', col_names)
        self.assertIsInstance(conditions.c.rule.type, sqlalchemy.types.String)
        self.assertIn('op', col_names)
        self.assertIsInstance(conditions.c.op.type, sqlalchemy.types.String)
        self.assertIn('multiple', col_names)
        self.assertIsInstance(conditions.c.multiple.type,
                              sqlalchemy.types.String)
        self.assertIn('field', col_names)
        self.assertIsInstance(conditions.c.field.type, sqlalchemy.types.Text)
        self.assertIn('params', col_names)
        self.assertIsInstance(conditions.c.params.type, sqlalchemy.types.Text)

        actions = db_utils.get_table(engine, 'rule_actions')
        col_names = [column.name for column in actions.c]
        self.assertIn('id', col_names)
        self.assertIsInstance(actions.c.id.type, sqlalchemy.types.Integer)
        self.assertIn('rule', col_names)
        self.assertIsInstance(actions.c.rule.type, sqlalchemy.types.String)
        self.assertIn('action', col_names)
        self.assertIsInstance(actions.c.action.type, sqlalchemy.types.String)
        self.assertIn('params', col_names)
        self.assertIsInstance(actions.c.params.type, sqlalchemy.types.Text)

    def _check_e169a4a81d88(self, engine, data):
        rule_conditions = db_utils.get_table(engine, 'rule_conditions')
        # set invert with default value - False
        data = {'id': 1, 'op': 'eq', 'multiple': 'all'}
        rule_conditions.insert().execute(data)

        conds = rule_conditions.select(
            rule_conditions.c.id == 1).execute().first()
        self.assertFalse(conds['invert'])

        # set invert with - True
        data = {'id': 2, 'op': 'eq', 'multiple': 'all', 'invert': True}
        rule_conditions.insert().execute(data)

        conds = rule_conditions.select(
            rule_conditions.c.id == 2).execute().first()
        self.assertTrue(conds['invert'])

    def test_upgrade_and_version(self):
        with patch_with_engine(self.engine):
            self.migration_ext.upgrade('head')
            self.assertIsNotNone(self.migration_ext.version())

    def test_upgrade_twice(self):
        with patch_with_engine(self.engine):
            self.migration_ext.upgrade('578f84f38d')
            v1 = self.migration_ext.version()
            self.migration_ext.upgrade('d588418040d')
            v2 = self.migration_ext.version()
            self.assertNotEqual(v1, v2)


class TestMigrationsMySQL(MigrationCheckersMixin,
                          WalkVersionsMixin,
                          test_base.MySQLOpportunisticTestCase):
    pass


class TestMigrationsPostgreSQL(MigrationCheckersMixin,
                               WalkVersionsMixin,
                               test_base.PostgreSQLOpportunisticTestCase):
    pass


class TestMigrationSqlite(MigrationCheckersMixin,
                          WalkVersionsMixin,
                          test_base.DbTestCase):
    pass


class ModelsMigrationSyncMixin(object):

    def get_metadata(self):
        return db.Base.metadata

    def get_engine(self):
        return self.engine

    def db_sync(self, engine):
        config = dbsync._get_alembic_config()
        config.ironic_inspector_config = CONF
        with patch_with_engine(engine):
            alembic.command.upgrade(config, 'head')


class ModelsMigrationsSyncMysql(ModelsMigrationSyncMixin,
                                test_migrations.ModelsMigrationsSync,
                                test_base.MySQLOpportunisticTestCase):
    pass


class ModelsMigrationsSyncPostgres(ModelsMigrationSyncMixin,
                                   test_migrations.ModelsMigrationsSync,
                                   test_base.PostgreSQLOpportunisticTestCase):
    pass


class ModelsMigrationsSyncSqlite(ModelsMigrationSyncMixin,
                                 test_migrations.ModelsMigrationsSync,
                                 test_base.DbTestCase):
    pass

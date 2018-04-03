import os
import os.path

import uuid
import jwt
import yaml
from authlib.common.encoding import to_unicode

from cirrus import GoogleCloudManager
from userdatamodel.driver import SQLAlchemyDriver
from userdatamodel.models import (
    AccessPrivilege,
    Bucket,
    CloudProvider,
    GoogleProxyGroup,
    Group,
    IdentityProvider,
    Project,
    StorageAccess,
    User,
)


from fence.models import Client
from fence.models import GoogleServiceAccount
from fence.models import UserRefreshToken
from fence.utils import create_client, drop_client
from fence.sync.sync_dbgap import DbGapSyncer

from fence.jwt.token import (
    issued_and_expiration_times,
)


def create_client_action(
        DB, username=None, client=None, urls=None, auto_approve=True):
    try:
        print(create_client(
            username, urls, DB, name=client, auto_approve=auto_approve))
    except Exception as e:
        print(e.message)


def delete_client_action(DB, client):
    try:
        drop_client(client, DB)
        print('Client {} deleted'.format(client))
    except Exception as e:
        print(e.message)


def sync_dbgap(projects):
    """
    sync ACL files from dbGap to auth db and storage backends
    imports from local_settings is done here because dbGap is
    an optional requirment for fence so it might not be specified
    in local_settings
    Args:
        projects: path to project_mapping yaml file which contains mapping
        from dbgap phsids to projects in fence database
    Returns:
        None
    Examples:
        the expected yaml structure sould look like:
        .. code-block:: yaml
            phs000178:
              - name: TCGA
                auth_id: phs000178
              - name: TCGA-PCAWG
                auth_id: TCGA-PCAWG
            phs000235:
              - name: CGCI
                auth_id: phs000235
    """
    from local_settings import dbGaP, STORAGE_CREDENTIALS, DB
    with open(projects, 'r') as f:
        project_mapping = yaml.load(f)
    syncer = DbGapSyncer(
        dbGaP, DB, project_mapping, storage_credentials=STORAGE_CREDENTIALS
    )
    print('sync')
    syncer.sync()


def create_sample_data(DB, yaml_input):
    with open(yaml_input, 'r') as f:
        data = yaml.load(f)

    db = SQLAlchemyDriver(DB)
    with db.session as s:
        create_cloud_providers(s, data)
        create_projects(s, data)
        create_group(s, data)
        create_users_with_group(DB, s, data)


def create_group(s, data):
    for group_name, fields in data['groups'].iteritems():
        projects = fields.get('projects', [])
        group = s.query(Group).filter(Group.name == group_name).first()
        if not group:
            group = Group(name=group_name)
        for project_data in projects:
            grant_project_to_group_or_user(s, project_data, group)


def create_projects(s, data):
    projects = data.get('projects', [])
    for project in projects:
        create_project(s, project)


def create_project(s, project_data):
    auth_id = project_data['auth_id']
    name = project_data.get('name', auth_id)
    project = s.query(Project).filter_by(name=name).first()
    if project is None:
        project = Project(name=name, auth_id=auth_id)
        s.add(project)
    if 'storage_accesses' in project_data:
        sa_list = project_data['storage_accesses']
        for storage_access in sa_list:
            provider = storage_access['name']
            buckets = storage_access.get('buckets', [])
            sa = (
                s.query(StorageAccess)
                .join(StorageAccess.provider, StorageAccess.project)
                .filter(Project.name == project.name)
                .filter(CloudProvider.name == provider).first()
            )
            if not sa:
                c_provider = (
                    s
                    .query(CloudProvider)
                    .filter_by(name=provider)
                    .first()
                )
                sa = StorageAccess(provider=c_provider, project=project)
                s.add(sa)
                print(
                    'created storage access for {} to {}'
                    .format(project.name, c_provider.name)
                )
            for bucket in buckets:
                b = (
                    s.query(Bucket)
                    .filter_by(name=bucket)
                    .join(Bucket.provider)
                    .filter(CloudProvider.name == provider)
                    .first()
                )
                print(b)
                if not b:
                    b = Bucket(name=bucket)
                    b.provider = c_provider
                    s.add(b)
                    print('created bucket {} in db'.format(bucket))

    return project


def grant_project_to_group_or_user(s, project_data, group=None, user=None):
    privilege = project_data['privilege']
    project = create_project(s, project_data)
    if group:
        ap = (
            s
            .query(AccessPrivilege).
            join(AccessPrivilege.project)
            .join(AccessPrivilege.research_group)
            .filter(Project.name == project.name, Group.name == group.name)
            .first()
        )
        name = group.name
    elif user:
        ap = (
            s
            .query(AccessPrivilege)
            .join(AccessPrivilege.project)
            .join(AccessPrivilege.user)
            .filter(
                Project.name == project.name,
                User.username == user.username,
            )
            .first()
        )
        name = user.username
    else:
        raise Exception('need to provide either a user or group')
    if not ap:
        if group:
            ap = AccessPrivilege(
                project=project, research_group=group, privilege=privilege
            )
        elif user:
            ap = AccessPrivilege(
                project=project, user=user, privilege=privilege
            )
        else:
            raise Exception('need to provide either a user or group')
        s.add(ap)
        print(
            'created access privilege {} of project {} to {}'
            .format(privilege, project.name, name)
        )
    else:
        ap.privilege = privilege
        print('updated access privilege {} of project {} to {}'
              .format(privilege, project.name, name))


def create_cloud_providers(s, data):
    cloud_data = data.get('cloud_providers', [])
    for name, fields, in cloud_data.iteritems():
        cloud_provider = s.query(CloudProvider).filter(
            CloudProvider.name == name
        ).first()
        if not cloud_provider:
            cloud_provider = CloudProvider(
                name=name, backend=fields.get('backend', 'cleversafe'),
                service=fields.get('service', 'storage')
            )
            s.add(cloud_provider)


def create_users_with_group(DB, s, data):
    providers = {}
    data_groups = data['groups']
    for username, data in data['users'].iteritems():
        is_existing_user = True
        user = s.query(User).filter(User.username == username).first()
        admin = data.get('admin', False)

        if not user:
            is_existing_user = False
            provider_name = data.get('provider', 'google')
            provider = providers.get(provider_name)
            if not provider:
                provider = s.query(IdentityProvider).filter(
                    IdentityProvider.name == provider_name).first()
                providers[provider_name] = provider
                if not provider:
                    raise Exception(
                        'provider {} not found'.format(provider_name))

            user = User(
                username=username, idp_id=provider.id, is_admin=admin
            )
        user.is_admin = admin
        group_names = data.get('groups', [])
        for group_name in group_names:
            assign_group_to_user(s, user, group_name, data_groups[group_name])
        projects = data.get('projects', [])
        for project in projects:
            grant_project_to_group_or_user(s, project, user=user)
        if not is_existing_user:
            s.add(user)
        for client in data.get('clients', []):
            create_client_action(DB, username=username, **client)


def assign_group_to_user(s, user, group_name, group_data):
    group = s.query(Group).filter(Group.name == group_name).first()
    if not group:
        group = Group(name=group_name)
        s.add(group)
        user.groups.append(group)
    if group not in user.groups:
        user.groups.append(group)


def google_init(db):
    # Initial user proxy group creation
    db = SQLAlchemyDriver(db)
    with db.session as s:
        users_without_proxy = (
            s.query(User).filter(User.google_proxy_group == None)
        )

        for user in users_without_proxy:
            with GoogleCloudManager() as g_mgr:
                response = g_mgr.create_proxy_group_for_user(
                    user.id, user.username)

                group = response["group"]
                primary_service_account = response["primary_service_account"]
                user.google_proxy_group_id = group["id"]

                # Add user's primary service account to database
                service_account = GoogleServiceAccount(
                    google_unique_id=primary_service_account["uniqueId"],
                    client_id=None,
                    user_id=user.id,
                    email=primary_service_account["email"]
                )

                proxy_group = GoogleProxyGroup(
                    id=group["id"],
                    email=group["email"]
                )

                s.add(service_account)
                s.add(proxy_group)
                s.commit()


def remove_expired_google_service_account_keys(db):
    db = SQLAlchemyDriver(db)
    with db.session as s:
        client_service_accounts = (
            s.query(GoogleServiceAccount, Client).filter(
                GoogleServiceAccount.client_id == Client.client_id)
        )

        with GoogleCloudManager() as g_mgr:
            for service_account, client in client_service_accounts:
                g_mgr.handle_expired_service_account_keys(
                    service_account.google_unique_id)


def delete_users(DB, usernames):
    driver = SQLAlchemyDriver(DB)
    with driver.session as session:
        # NOTE that calling ``.delete()`` on the query itself will not follow
        # cascade deletion rules set up in any relationships.
        users_to_delete = (
            session
            .query(User)
            .filter(User.username.in_(usernames))
            .all()
        )
        for user in users_to_delete:
            session.delete(user)
        session.commit()


def get_jwt_keypair(kid, root_dir):

    from fence.settings import JWT_KEYPAIR_FILES
    # cur_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    # par_dir = os.path.abspath(os.path.join(cur_dir, os.pardir))
    private_key = None

    if len(JWT_KEYPAIR_FILES) == 0:
        return None, None

    private_filepath = None
    if kid is None:
        private_filepath = os.path.join(
            root_dir, JWT_KEYPAIR_FILES.values()[0][1])
    else:
        for _kid, (_, private) in JWT_KEYPAIR_FILES.iteritems():
            if(kid != _kid):
                continue
            private_filepath = os.path.join(root_dir, private)

    if private_filepath is None:
        return None, None

    try:
        with open(private_filepath, 'r') as f:
            private_key = f.read()
    except IOError:
        private_key = None

    if kid:
        return kid, private_key
    else:
        return JWT_KEYPAIR_FILES.keys()[0], private_key


def create_user_token(DB, BASE_URL, ROOT_DIR, kid, token_type, username, scopes, expires_in=3600):
    try:
        if token_type == 'access_token':
            _, token, _ = create_user_access_token(
                DB, BASE_URL, ROOT_DIR, kid, username, scopes, expires_in)
            return token
        elif token_type == 'refresh_token':
            _, token, _ = create_user_refresh_token(
                DB, BASE_URL, ROOT_DIR, kid, username, scopes, expires_in)
            return token
        else:
            print('=============Option type is wrong!!!. Please select either access_token or refresh_token=============')
            return None
    except Exception as e:
        print(e.message)
        return None


def create_user_refresh_token(DB, BASE_URL, ROOT_DIR, kid, username, scopes, expires_in=3600):
    kid, private_key = get_jwt_keypair(kid=kid, root_dir=ROOT_DIR)
    if private_key is None:
        print("=========Can not find the private key !!!!==============")
        return None, None, None

    driver = SQLAlchemyDriver(DB)
    with driver.session as current_session:
        user = (current_session.query(User)
                .filter_by(username=username)
                .first()
                )
        if not user:
            print('=========user is not existed !!!=============')
            return None, None, None

        headers = {'kid': kid}
        iat, exp = issued_and_expiration_times(expires_in)
        jti = str(uuid.uuid4())
        sub = str(user.id)
        claims = {
            'pur': 'refresh',
            'aud': scopes.split(','),
            'sub': sub,
            'iss': BASE_URL,
            'iat': iat,
            'exp': exp,
            'jti': jti,
            'context': {
                'user': {
                    'name': user.username,
                    'is_admin': user.is_admin,
                    'projects': dict(user.project_access),
                },
            },
        }

        token = to_unicode(jwt.encode(claims, private_key,
                                      headers=headers, algorithm='RS256'), 'UTF-8')
        current_session.add(
            UserRefreshToken(
                jti=claims['jti'], userid=user.id, expires=claims['exp']
            )
        )
        current_session.commit()
        return jti, token, claims


def create_user_access_token(DB, BASE_URL, ROOT_DIR, kid, username, scopes, expires_in=3600):
    kid, private_key = get_jwt_keypair(kid=kid, root_dir=ROOT_DIR)
    if private_key is None:
        print("=========Can not find the private key !!!!=============")
        return None, None, None

    driver = SQLAlchemyDriver(DB)
    with driver.session as current_session:
        user = (current_session.query(User)
                .filter_by(username=username)
                .first()
                )
        if not user:
            print('=========user is not existed !!!=============')
            return None, None, None

        headers = {'kid': kid}
        iat, exp = issued_and_expiration_times(expires_in)
        jti = str(uuid.uuid4())
        sub = str(user.id)
        claims = {
            'pur': 'access',
            'aud': scopes.split(','),
            'sub': sub,
            'iss': BASE_URL,
            'iat': iat,
            'exp': exp,
            'jti': jti,
            'context': {
                'user': {
                    'name': user.username,
                    'is_admin': user.is_admin,
                    'projects': dict(user.project_access),
                },
            },
        }

        return jti, to_unicode(jwt.encode(claims, private_key, headers=headers, algorithm='RS256'), 'UTF-8'), claims
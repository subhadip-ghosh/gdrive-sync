from os import path
from watchdog import observers
from gdrive_sync import utils, LocalFSEventHandler, Db
import time
import os

logger = utils.create_logger(__name__)


class GdriveSync:
    """
    This class is responsible for doing various drive
    specific tasks.
    """

    def __init__(self):
        self._local_dir_observer_dict = {}
        self._db_handler = Db.DbHandler()

    def _process_dir_pairs(self, service, dir_pairs):
        """
        Syncs the local and remote dirs with each other.
        Also saves the local_dir_paths and remote_dir_ids in the Db

        Args:
            service: A googleapiclient.discovery.Resource object
            dir_pairs = A Dict of local dirs and remote dirs. It can be obtained by below:
                "utils.get_user_settings()['synced_dirs']"
        """
        for local_dir, remote_dir in dir_pairs.items():
            remote_dir = utils.get_remote_dir(service,
                                              'root',
                                              remote_dir.split('/')[1:])
            self._db_handler.insert_record(local_dir,
                                           remote_dir['id'],
                                           os.stat(local_dir).st_mtime,
                                           utils.convert_rfc3339_time_to_epoch(remote_dir['modifiedTime']))
            remote_files_under_dir = utils.list_remote_files_from_dir(service,
                                                                      remote_dir['id'])
            local_files_under_dir = utils.list_files_under_local_dir(local_dir)
            self._compare_and_sync_files(service,
                                         remote_files_under_dir,
                                         remote_dir['id'],
                                         local_files_under_dir,
                                         local_dir)

    def _compare_and_sync_files(self,
                                service,
                                remote_files,
                                remote_parent_dir_id,
                                local_files,
                                local_parent_dir):
        """
        Compares the local and remote files by name and modification date
        and whichever is last modified replaces the other one with same name.

        It also saves the local_file_paths and remote_file_ids to Db.

        TODO: If it's a directory, then the containing files are compared instead.

        Args:
            service: A googleapiclient.discovery.Resource object
            remote_files: A generator of objects has the below format
                {
                'id': 'A String' that represents the id of the remote file
                'name': 'A String' that represents the name of the remote file
                'modifiedTime': 'A String' that represents the last modifiedTime
                    of the remote file in rfc3339 format
                }
            remote_parent_dir_id: 'A String' representing the parent dir id for the remote_files
            local_files: A list of os.DirEntry
            local_parent_dir: 'A String' representing the parent dir for the local_files
        """
        local_file_dict = {}
        for file in local_files:
            if type(file) == dict:
                key = next(iter(file))
                local_file_dict[key.name] = file[key]
            else:
                local_file_dict[file.name] = file

        for each_remote_entry in remote_files:

            # If remote file is a dir
            if each_remote_entry['mimeType'] == 'application/vnd.google-apps.folder':
                tmp_local_files = []

                # If remote dir is not created in local
                if not each_remote_entry['name'] in local_file_dict:
                    local_dir_path = path.join(local_parent_dir, each_remote_entry['name'])

                    if self._db_handler.get_local_file_path(each_remote_entry['id']):
                        logger.debug('Dir %s was removed from local.', local_dir_path)

                        utils.delete_file_on_remote(each_remote_entry['id'])

                        self._db_handler.delete_record(local_dir_path)

                        continue
                    else:
                        logger.debug('Creating dir %s in local.', local_dir_path)

                        utils.create_local_dir(local_dir_path)

                        self._db_handler.insert_record(local_dir_path,
                                                       each_remote_entry['id'],
                                                       int(time.time()),
                                                       utils.convert_rfc3339_time_to_epoch(
                                                           each_remote_entry['modifiedTime']))
                else:
                    tmp_local_files = local_file_dict[each_remote_entry['name']]
                    del local_file_dict[each_remote_entry['name']]

                self._compare_and_sync_files(service,
                                             each_remote_entry['children'],
                                             each_remote_entry['id'],
                                             tmp_local_files,
                                             os.path.join(local_parent_dir, each_remote_entry['name']))

            # If remote file exists in local
            elif each_remote_entry['name'] in local_file_dict:
                local_file = local_file_dict[each_remote_entry['name']]

                remote_file_modified_time = utils.convert_rfc3339_time_to_epoch(
                    each_remote_entry['modifiedTime'])

                # If local file modification time is newer than remote file modification time
                if local_file.stat().st_mtime > remote_file_modified_time:

                    local_modification_date_in_db = self._db_handler.get_local_modification_date(local_file.path)
                    actual_local_modification_date = int(local_file.stat().st_mtime)

                    # If local file modification time is newer than saved in db else don't do anything
                    # This cancels the cases where remote file was earlier copied to local
                    if (not local_modification_date_in_db or
                            actual_local_modification_date > local_modification_date_in_db):
                        logger.debug('local_file.stat().st_mtime %s, local_modification_date_in_db %s.',
                                     local_file.stat().st_mtime,
                                     local_modification_date_in_db)
                        logger.debug('Overwriting %s in remote.', local_file.path)

                        utils.overwrite_remote_file_with_local(service,
                                                               each_remote_entry['id'],
                                                               local_file.path)

                        self._db_handler.insert_record(local_file.path,
                                                       each_remote_entry['id'],
                                                       actual_local_modification_date,
                                                       int(time.time()))

                # If remote file modification time is newer than local file modification time
                elif remote_file_modified_time > local_file.stat().st_mtime:

                    remote_file_modification_time_in_db = self._db_handler.get_remote_modification_date(
                        each_remote_entry['id'])

                    # If remote file modification time is newer than saved in db
                    # This cancels the cases where local file was earlier copied to remote
                    if (not remote_file_modification_time_in_db or
                            remote_file_modified_time > remote_file_modification_time_in_db):
                        logger.debug('remote_file_modified_time %s, remote_file_modification_time_in_db %s.',
                                     remote_file_modified_time,
                                     remote_file_modification_time_in_db)
                        logger.debug('Overwriting %s in local.', local_file.path)

                        utils.copy_remote_file_to_local(service,
                                                        local_file.path,
                                                        each_remote_entry['id'])

                        self._db_handler.insert_record(local_file.path,
                                                       each_remote_entry['id'],
                                                       int(time.time()),
                                                       remote_file_modified_time)
                del local_file_dict[each_remote_entry['name']]

            else:  # remote file does not exist in local

                local_file_path = path.join(local_parent_dir, each_remote_entry['name'])

                if self._db_handler.get_local_file_path(each_remote_entry['id']):
                    logger.debug('File %s was removed from local.', local_file_path)

                    utils.delete_file_on_remote(each_remote_entry['id'])

                    self._db_handler.delete_record(local_file_path)

                else:
                    logger.debug('Creating %s in local.', local_file_path)

                    utils.copy_remote_file_to_local(service,
                                                    local_file_path,
                                                    each_remote_entry['id'])
                    self._db_handler.insert_record(local_file_path,
                                                   each_remote_entry['id'],
                                                   int(time.time()),
                                                   utils.convert_rfc3339_time_to_epoch(
                                                       each_remote_entry['modifiedTime']))

        # copy the local files that do not exist at remote
        self._copy_local_to_remote(local_file_dict, remote_parent_dir_id, service)

    def _copy_local_to_remote(self, local_file_dict, remote_parent_dir_id, service):
        for file_name, local_file in local_file_dict.items():

            if type(local_file) == dict:
                dir_key = next(iter(local_file))

                if self._db_handler.get_remote_file_id(dir_key.path):
                    logger.debug('Remote dir %s was deleted.', dir_key.path)
                    utils.delete_file_from_local(dir_key.path)
                    self._db_handler.delete_record(dir_key.path)

                else:
                    logger.debug('Creating dir %s at remote.', dir_key.path)
                    remote_dir_id = utils.create_remote_dir(file_name, remote_parent_dir_id)
                    self._db_handler.insert_record(dir_key.path,
                                                   remote_dir_id,
                                                   dir_key.stat().st_mtime,
                                                   int(time.time()))
                    self._copy_local_to_remote({file.name: file for file in local_file[dir_key]},
                                               remote_dir_id,
                                               service)
            else:
                if self._db_handler.get_remote_file_id(local_file.path):
                    logger.debug('Remote dir %s was deleted.', local_file.path)
                    utils.delete_file_from_local(local_file.path)
                    self._db_handler.delete_record(local_file.path)

                else:
                    logger.debug('Creating file %s at remote.', local_file.path)

                    remote_file_id = utils.copy_local_file_to_remote(local_file.path,
                                                                     remote_parent_dir_id,
                                                                     service)
                    self._db_handler.insert_record(local_file.path,
                                                   remote_file_id,
                                                   local_file.stat().st_mtime,
                                                   int(time.time()))

    def sync_onetime(self, synced_dirs_dict):
        """
        Collects the local vs remote directory mappings from user directory and
        synchronizes the local and remote directories.

        Args:
            synced_dirs_dict: A dict comprises of local vs remote dir pairs from settings
        """
        service = utils.get_service()
        self._process_dir_pairs(service, synced_dirs_dict)

    def _watch_local_dir(self, dir_to_watch):
        """
        It listens for the given directory and if any file/directory change event happens, it syncs
        the change with the remote. It returns the observer object after starting it.
        It should be run after _compare_and_sync_files to populate the Db first.
        Args:
            dir_to_watch: 'A String' that represents the path to the directory to watch
        Returns:
            An object of watchdog.observers.Observer.
        """
        event_handler = LocalFSEventHandler.LocalFSEventHandler(self._db_handler)
        observer = observers.Observer()
        observer.schedule(event_handler, dir_to_watch, recursive=True)
        observer.start()
        return observer

    def start_sync(self):  # TODO: Write test
        """
        This is the method to be invoked for starting the sync.

        Args:
            synced_dir_pairs: A dict comprises of local dir path in String vs
                remote dir path in String pairs
        Returns:
            A dict of local_dir_path in String and the observer.Observer objects
        """
        synced_dirs_from_settings = utils.get_user_settings()['synced_dirs']
        self.sync_onetime(synced_dirs_from_settings)
        for local_dir in synced_dirs_from_settings.keys():
            self._local_dir_observer_dict[local_dir] = self._watch_local_dir(local_dir)

    def stop_sync(self):  # TODO: Write test
        """
        This is to stop the sync and shutdown all the observers
        """
        for observer in self._local_dir_observer_dict.keys():
            observer.stop()

# iCloud Drive download

Given the following links:

- https://github.com/boredazfcuk/docker-icloudpd
- https://github.com/mandarons/icloud-docker
- https://github.com/icloud-photos-downloader/icloud_photos_downloader

Create a minimal, non-root, Docker container to download files from an iCloud drive.

## Docker container

The Docker container must:

- use Alpine Linux as its base unless this is not feasible
- accept user and group values for the container user and group, with robust defaults of 1000 for each;
- run with minimal privileges;
- allow a specified username within the container, for use with Telegram messaging;
- accept a TZ environment variable to be used by the container;
- allow incremental backup as efficiently as possible;
- allow for multiple containers in the same compose project;
- allow for a delayed start to containers, to avoid flooding the API when running multiple containers in a project;
- allow passing docker secrets for a user's iCloud email and password;
- use an iCloud app specific password (preferred) or a user's iCloud account password to authenticate alongside multi-factor authentication;
- allow Telegram notifications of backups starting and finishing, including number of files transferred out of the total number of files in the drive;
- allow Telegram authentication and reauthentication;
- store a user's iCloud credentials in the keychain to avoid reentry until reauthentication is required;
- use the same cookie files as boredazfcuk/docker-icloudpd if possible to allow interoperability;
- allow a user to trigger a one-off incremental backup using "<username> backup", where username matches the username within the container;
- implement a performant safety net, that stops backups, for users running this over the top of a backup created using something like mandarons/icloud-docker, such that permissions of the existing downloaded files – though likely not checking all files – are checked before starting a full backup to overwrite existing files;
- ensure the safety net is only applied on first run and that the safety net tells the user explicitly the permissions that would match existing files, via Telegram and in the container logs; and
- if possible, use https://github.com/tarampampam/microcheck?cmdf=microcheck+docker as a healthcheck.

## Authentication

For authentication and reauthentication via Telegram, the container must:

- prompt on initial run to authenticate with an app specific password, or account password, as well as multi-factor authentication
- subsequently alert the user that reauthentication via multi-factor authentication is required within five days
- prompt the user to reauthenticate using multi-factor authentication when it is required within two days
- allow for authentication and reauthentication edge cases by allowing a user to message Telegram with "<username> auth" or "<username> reauth", where username matches the username within the container

## Additional considerations

The mandarons/icloud-docker container can take a very long time to backup a drive, over ten hours, as it traverses the entire drive each time, even though backups are incremental. The boredazfcuk/docker-icloudpd container, though only downloading photos, allows a user to set a limit on how many already downloaded files it finds before stopping.

Whilst the boredazfcuk solution is not applicable to a folder structure as it is linear/date based, explore the most optimal and performant solution for backing up a drive, e.g. a manifest file, so that it is not necessary to traverse the entire drive on each backup.
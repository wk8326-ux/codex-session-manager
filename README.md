# Local Project Console

Run `start-console.bat`, then open `http://127.0.0.1:8765`.

Register each project with its working directory, start command, optional stop command, port, and local URL. The dashboard checks whether the configured port is listening and whether a process started by the dashboard is still alive.

Projects that already run on a server can use the `External website` mode. They
need only an HTTP or HTTPS URL and appear as an `Open` action. The console checks
the URL on a short interval and reports whether the remote website is online; it
does not pretend to start or stop the remote service.

The project definitions are saved in `projects.json` after the first launch. Command output is appended to `logs/<project-id>.log`.

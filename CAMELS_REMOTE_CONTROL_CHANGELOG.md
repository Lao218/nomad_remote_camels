# CAMELS--OASIS Remote Control Changes

## `.dockerignore`: local build context and credential protection

The `.dockerignore` file controls which files Docker sends to the Docker
daemon when building the local OASIS image. It is not part of the running
application and it does not delete any local files.

For the current development deployment, it has two purposes:

- It includes the local `nomad-gui/` checkout because the Dockerfile builds
  the experimental GUI v2 dashboard from this source tree.
- It excludes unnecessary or sensitive files, including the GUI Git history,
  `node_modules`, generated GUI build output, `.volumes/` (which can contain
  encrypted CAMELS connection credentials), and run logs. This keeps the
  Docker build context smaller and avoids copying credentials into an image.

## `.gitignore`: local credentials and GUI source

The `.gitignore` file was extended to keep local CAMELS bridge data and the
local GUI v2 checkout out of the OASIS distribution repository:

- `.volumes/fs/camels-bridge/settings.json` is ignored because it contains
  encrypted, user-specific CAMELS connection records and API keys. It must
  remain local and must never be committed to Git.
- `.run-logs/` is ignored because it contains transient local runtime logs
  rather than source code required to reproduce the deployment.
- `/nomad-gui/` is ignored because it is a separate NOMAD GUI Git checkout
  used only as local Docker build input. Ignoring it prevents its large source
  tree and independent Git history from being accidentally added to this OASIS
  repository.

## `Dockerfile`: build the local OASIS plugin and GUI v2

The Docker build was extended so that the local CAMELS--OASIS bridge and the
locally modified NOMAD GUI v2 are included in the development image. Line
numbers below refer to the current `Dockerfile`.

- **Lines 83--84:** `COPY nomad_camels_oasis ./nomad_camels_oasis` makes the
  local plugin source available in the Python builder stage before `uv sync
  --extra plugins` runs. This allows the distribution build to install the
  locally developed bridge.
- **Lines 91--105:** A separate `gui_builder` stage based on
  `node:24-bookworm-slim` builds the local `nomad-gui/` checkout. It copies
  `package.json` and `package-lock.json`, runs `npm ci --ignore-scripts` for
  reproducible cached dependencies, then copies the source and runs the GUI
  post-install and build commands.
- **Line 103:** The GUI build uses `--base /prefix-to-replace`, which leaves a
  placeholder base path for NOMAD to replace when it serves GUI v2 under its
  deployment URL prefix.
- **Lines 104--105:** Before copying the newly built assets, the temporary GUI
  static-assets directory is cleared. This prevents stale assets from earlier
  GUI builds from being mixed with the current dashboard build.
- **Lines 173 and 177:** The final image receives the bridge source with
  ownership assigned to the unprivileged `nomad` user, and receives only the
  compiled GUI static files from `gui_builder`. Node.js, `node_modules`, and
  the complete GUI Git checkout are not included in the final runtime image.

## `configs/nomad.yaml`: register the CAMELS bridge plugin

**Lines 15--20** add the `nomad_camels_oasis.entry_points:camels` plugin entry
point to the local OASIS configuration.

- `camels_base_url: "http://host.docker.internal:8000"` provides the default
  CAMELS API address as reachable from the NOMAD application container. In the
  current Docker Desktop deployment, `host.docker.internal` resolves from the
  OASIS container to the machine running CAMELS. A real server deployment must
  replace this with the reachable hostname or IP address of the CAMELS host.
- `request_timeout_seconds: 10` sets the default upper limit for an OASIS to
  CAMELS HTTP request before the bridge reports a connection failure.
- The comments document that API keys are configured through the CAMELS setup
  page and may alternatively be supplied through the `CAMELS_API_KEY`
  environment variable for unattended deployments. No API key is stored in
  this YAML file.

## `docker-compose.yaml`: run CAMELS Actions with the custom OASIS image

The Compose deployment was updated to start an Action worker and to use the
custom distribution image that contains the CAMELS--OASIS bridge. Line numbers
below refer to the current `docker-compose.yaml`.

- **Lines 257--280:** Enable the `cpu_worker` service for plugin-defined
  NOMAD Actions, including CAMELS measurement runs. The worker uses the custom
  `ghcr.io/lao218/nomad_remote_camels/cpu-action:main` image, waits until
  Elasticsearch, MongoDB, and Temporal are healthy, shares the OASIS file
  volume, and starts `action-cpu-worker`. Without a running CPU Action worker,
  a Dashboard request can create an Action but it remains in `processing`
  because no worker executes it.
- **Lines 284--287:** Replace the upstream distribution image for the `app`
  service with `ghcr.io/lao218/nomad_remote_camels:main`. This custom image is
  built from this repository and contains the local `nomad_camels_oasis`
  plugin and the compiled GUI v2 dashboard changes.
- **Line 311:** Change the NOMAD application from two Gunicorn workers to one
  worker. This keeps the experimental local bridge's process-local GUI/API
  state on one application process during this deployment. It is a deployment
  constraint for the current implementation and should be revisited if the
  bridge state is later moved to shared persistent storage.

## `nomad_camels_oasis/__init__.py`: bridge package marker

 adds the root Python package marker and its short description:
`"Secure OASIS-to-CAMELS API bridge."`  It makes `nomad_camels_oasis` an
importable package and documents the package's responsibility.

## `nomad_camels_oasis/actions/__init__.py`: Actions package marker

adds the `actions` subpackage marker and documents that this
subpackage contains the NOMAD Action implementation for CAMELS measurement
requests. It enables imports such as `nomad_camels_oasis.actions.activities`.

## `nomad_camels_oasis/actions/activities.py`: Temporal-to-CAMELS activities

This new file defines the Temporal activities executed by the OASIS CPU Action
worker. Line numbers below refer to the current file.

- **Lines 1--12:** Import the HTTP client, Temporal activity decorator,
  encrypted credential store, and typed CAMELS Action input model.
- **Lines 15--47 (`_connection_for_protocol`):** Load the requesting user's
  encrypted CAMELS connections from the server-side settings file. It first
  supports legacy protocol-scoped records. For a current global CAMELS API
  key, it calls `GET /api/v1/protocols` with `Authorization: Bearer <api key>`
  and accepts the key only when CAMELS reports that the requested protocol is
  exposed to OASIS remote control. This prevents a saved connection belonging
  to another user, or a non-exposed protocol, from being used.
- **Lines 49--60 (`start_camels_run`):** Define a Temporal activity that sends
  an authenticated `POST` request to CAMELS'
  `/api/v1/actions/run/protocols/{protocol_name}` endpoint. The request carries
  the requested variable values and returns CAMELS' run response to the
  workflow.
- **Lines 62--71 (`get_camels_run_status`):** Define a Temporal activity that
  sends an authenticated `GET` request to CAMELS'
  `/api/v1/protocols/results/{run_id}` endpoint. The workflow uses the
  returned result to determine whether the measurement is still running,
  completed, or failed.

## `nomad_camels_oasis/actions/models.py`: typed CAMELS Action input

This new file defines the validated input passed from a NOMAD Action to the
CAMELS Temporal workflow.

- **Lines 1--3:** Import the generic value type and Pydantic validation tools.
- **Lines 6--12 (`CAMELSMeasurementInput`):** Define the measurement request
  model. It requires the owning NOMAD `user_id` and a non-empty
  `protocol_name`, accepts arbitrary protocol `variables`, and optionally
  accepts `upload_id`. The optional upload ID keeps compatibility with
  ELN-triggered Actions, while Dashboard-triggered Actions are user-owned but
  have no associated upload.

## `nomad_camels_oasis/actions/workflows.py`: CAMELS measurement workflow

This new file defines the Temporal workflow that coordinates one remote CAMELS
measurement. Line numbers below refer to the current file.

- **Lines 1--11:** Import Temporal workflow and retry primitives. The activity
  imports are placed in `workflow.unsafe.imports_passed_through()` because the
  imported HTTP activity code must execute in the worker, not be replayed as
  deterministic workflow code.
- **Lines 14--18:** Define `CAMELSMeasurementWorkflow` and configure up to
  three attempts for each external CAMELS activity.
- **Lines 19--25:** Execute `start_camels_run` with a 30-second timeout, then
  extract the returned run identifier. The fallback field names maintain
  compatibility with earlier CAMELS API response formats.
- **Lines 26--27:** Return the raw start response when CAMELS does not provide
  a run identifier, rather than failing or attempting invalid status requests.
- **Lines 28--38:** Poll the CAMELS status activity every two seconds, for at
  most 3,600 iterations (approximately two hours), using the same user and
  protocol context to re-check authorization on every request.
- **Lines 39--55:** Recognize CAMELS terminal states. In addition to explicit
  status words such as `finished`, `failed`, and `aborted`, CAMELS can replace
  its temporary running status with the final HDF5/NeXus result-file path. A
  path containing `/` or `\\` is therefore correctly treated as successful
  completion instead of being polled forever.
- **Lines 56--59:** Return a monitoring-timeout result when the two-hour
  polling limit is reached.

## `nomad_camels_oasis/app.py`: authenticated OASIS--CAMELS bridge

This new 1,602-line module is the OASIS-side FastAPI application. It provides
the authenticated setup page, measurement dashboard, encrypted per-user
connection storage, CAMELS API proxy, NOMAD Action integration, and remote
plot rendering. Line numbers below refer to the current file.

### Data models, credential protection, and plot limits

- **Lines 1--23:** Import FastAPI, NOMAD authentication, encrypted-storage,
  HTTP-proxy, and browser-response dependencies.
- **Lines 26--56:** Define a 2,000-point presentation limit for Browser Plot
  responses. The helpers evenly downsample ordinary plot arrays and stored fit
  traces while preserving both endpoints. The original complete measurement
  remains in CAMELS/NOMAD.
- **Lines 59--82:** Define validated request models for direct runs, saved
  protocol parameters, Dashboard-created Actions, and CAMELS connections.
- **Lines 85--116 (`CredentialStore`):** Encrypt connection data at rest using
  a Fernet key derived from `NOMAD_SERVICES_API_SECRET`. Reads report a safe
  server error if the file cannot be decrypted; writes use a temporary file and
  atomic replacement to avoid corrupting the saved credentials file.

### Setup and Dashboard browser UI

- **Lines 119--188:** Define the initial Setup and Dashboard HTML/JavaScript,
  then add Browser Plot configuration, native CAMELS plot-window monitoring,
  and PID-controller Plotly rendering. Browser-side code never receives a
  CAMELS API key; it talks only to authenticated OASIS routes.
- **Lines 191--474:** Modify the Setup page at build time: replace modal
  confirmation with an iframe-compatible two-click delete; display saved
  connections as a non-secret service-information table; show readable errors;
  link to GUI v2; add delete-all confirmation; add protocol selection,
  parameter loading/saving, and Dashboard Action start/status controls; refresh
  the protocol list after saving a connection; and use POST deletion routes.
- **Lines 479--676:** Replace direct Browser Plot iframe access with Plotly
  rendering from authenticated OASIS proxy snapshots. This allows remote OASIS
  deployments to show CAMELS Browser Plots without requiring the user's
  browser to reach a CAMELS localhost port. It also renders stored fit traces,
  supports manual or automatic clearing, and polls plot data only while a
  measurement is active.
- **Lines 678--703:** When the Dashboard opens, select the protocol currently
  running in CAMELS. If no run is active, retain the normal first protocol
  selection.
- **Lines 706--820:** Add the aligned Start measurement button and live
  measurement-status panel. It mirrors the CAMELS progress snapshot, shows the
  active protocol, disables starting while a measurement or Action is active,
  clears visible Browser Plot data before a new Action, creates a real NOMAD
  Action, and polls its completion state.
- **Lines 822--935:** Retain an experimental native Qt plot-window mirroring
  implementation behind an immediate JavaScript `return`. It is disabled and
  not part of the active Dashboard because its cross-thread live-data approach
  was unstable.
- **Lines 937--966:** Retain a lightweight iframe availability-retry helper
  for the earlier Browser Plot iframe implementation. The active Dashboard
  now uses the snapshot renderer above, so this legacy helper can be removed
  in a future cleanup if iframe rendering is not restored.

### Server-side bridge, isolation, and authentication

- **Lines 969--1005 (`create_app` and exception handler):** Create the bridge
  FastAPI application and return the same Authentication required HTML page
  for redirected Setup/Dashboard pages after a logout, rather than raw JSON.
- **Lines 1008--1132:** Read and store encrypted connection records by NOMAD
  `user_id`. Legacy installation-wide credentials are preserved only for a
  manual administrator migration and are never used for API requests. One
  current CAMELS API key is stored per CAMELS server; CAMELS itself determines
  which protocols are exposed.
- **Lines 1134--1177:** Build a protocol catalogue from each user's saved
  connections. For current global keys, query CAMELS' exposed-protocol list;
  protocol-scoped legacy records remain supported without allowing an old key
  to override a newly saved connection.
- **Lines 1179--1216 (`request_camels`):** Provide the common authenticated
  HTTP proxy helper. It sends Bearer credentials only server-to-server,
  serializes JSON payloads, honours the configured timeout, and translates
  CAMELS HTTP/network errors into safe FastAPI errors.
- **Lines 1219--1240:** Add a logout cleanup endpoint that clears browser
  cache, cookies, and storage through `Clear-Site-Data`, then explicitly
  removes legacy Authorization cookies on all relevant NOMAD paths.

### Authenticated API routes

- **Lines 1242--1300:** Protect Setup and connection-management routes with
  NOMAD read/write scopes. Users can test, save, list, delete one, or delete
  all of only their own encrypted CAMELS connections.
- **Lines 1303--1363:** Protect Dashboard routes for connection discovery,
  exposed protocol listing, protocol schema/definition retrieval, persistent
  parameter saving, and direct CAMELS run compatibility.
- **Lines 1365--1394:** Create a user-owned NOMAD Action with `start_action`
  and provide its Action status. The protocol-to-connection permission check is
  performed before the asynchronous workflow is scheduled.
- **Lines 1396--1427:** Proxy run status, CAMELS native plot-window count,
  Qt-independent plot caches, and the thread-safe CAMELS measurement-status
  snapshot for the selected authorized protocol.
- **Lines 1429--1517:** Implement the remote Browser Plot proxy. It forwards
  GET/POST requests through OASIS to CAMELS' authenticated localhost-only
  browser-plot proxy, applies the point limit to `/data`, and rewrites Dash
  root-relative asset and callback paths so they continue through the OASIS
  route rather than the browser's localhost.
- **Lines 1519--1564:** Detect a currently active exposed measurement, proxy a
  request to close CAMELS plot windows, and proxy PID-controller plot data.
- **Lines 1566--1600:** Provide the smaller legacy-compatible health,
  protocol, schema, run, and run-status routes, all protected by the same
  user-scoped connection lookup. These routes overlap with the newer
  Dashboard/Action flow and may be removed in a future cleanup once it is
  confirmed that no legacy page, script, or external API client depends on
  them. In particular, the direct `POST /runs` route bypasses creation of a
  NOMAD Action and should not be used by the current Dashboard.

## `nomad_camels_oasis/entry_points.py`: NOMAD plugin registration

This new file declares the NOMAD plugin entry points that make the bridge,
Action, schema, and Dashboard pages discoverable by NOMAD. Line numbers below
refer to the current file.

- **Lines 1--14:** Import NOMAD entry-point base classes and configuration
  helpers.
- **Lines 17--55 (`CAMELSAPIEntryPoint`):** Define the bridge API plugin
  configuration: the default CAMELS base URL, request timeout, and encrypted
  settings-file path. Its `load` method constructs the FastAPI application
  from `app.py` using deployment configuration and server environment secrets.
- **Lines 27--42 (`dict_safe`):** Add
  `documentation_absolute_url` to the safe API catalogue representation. GUI
  v2 uses this field to enable the documentation button and opens the bridge
  Swagger UI at `<api_base_path>/camels/docs`.
- **Lines 58--61 (`camels`):** Register the API in NOMAD as **CAMELS remote
  control** at the `/camels` prefix.
- **Lines 63--68:** Register the CAMELS Measurement ELN schema package.
- **Lines 70--85 (`CAMELSMeasurementActionEntryPoint`):** Register the
  measurement workflow and its two Temporal activities on NOMAD's CPU task
  queue. This is what connects a Dashboard-created NOMAD Action to the worker
  that starts and monitors CAMELS.
- **Lines 88--103:** Register two separate Dashboard entry points: the
  connection Setup page and the measurement-monitor page.
- **Lines 106--128:** Instantiate the schema, Action, Setup Dashboard, and
  Monitor Dashboard registrations with their names, descriptions, URL-safe
  IDs, and embedded/tab launch modes.

## `nomad_camels_oasis/measurement_dashboard.py`: authenticated Dashboard mounts

This new file provides the small Dashboard applications registered by the
plugin entry points. Line numbers below refer to the current file.

- **Lines 1--12:** Import FastAPI/NOMAD authentication dependencies and define
  a non-anonymous NOMAD read-user dependency. Authentication is enforced at
  the Dashboard mount itself, rather than only after the embedded page calls
  the `/camels` API.
- **Lines 14--21:** Define the shared Authentication required HTML page. It
  uses a black lock icon consistent with the GUI v2 Actions lock screen and a
  sign-in action that either activates the parent GUI login control or returns
  the user to GUI v2.
- **Lines 24--52 (`_redirect_dashboard`):** Create a protected redirect app.
  It converts 401 errors into the shared lock page, returns ordinary JSON for
  other API errors, and redirects authenticated users without caching. The
  redirect avoids nesting a second iframe inside GUI v2's existing dashboard
  iframe.
- **Lines 55--61:** Create separate redirect mounts for CAMELS connection
  setup (`/camels/setup`) and the measurement monitor (`/camels/dashboard`).

## `nomad_camels_oasis/measurement_schema.py`: CAMELS Measurement ELN schema

This new optional ELN path creates a CAMELS NOMAD Action only after an explicit
user request. Line numbers below refer to the current file.

- **Lines 1--13:** Import NOMAD ELN, Action, JSON, and typed Action-input
  dependencies and initialize the schema package.
- **Lines 16--46 (`CAMELSMeasurement` quantities):** Define ELN fields for the
  exposed protocol name, JSON variable values, measurement lifecycle status,
  explicit Start CAMELS measurement control, workflow ID/status, and CAMELS
  run ID.
- **Lines 48--92 (`normalize`):** Update an existing Action status when an ELN
  entry is normalized. Only when the user explicitly starts the measurement or
  marks it ready, validates the JSON variables and requires an author/upload,
  it calls NOMAD `start_action`. The method then stores the workflow ID, marks
  the request queued, and resets the one-shot start control. Invalid JSON is
  marked as failed without preventing the ELN from being saved.
- **Line 93:** Finalize NOMAD metainfo registration for the schema package.

## `nomad_camels_oasis/start_local_camels_dashboard.py`: local development launcher

This new convenience script starts the local Docker-based development stack.

- **Lines 1--9:** Document that it must be run from the repository context and
  requires Docker Desktop.
- **Lines 17--18:** Define the repository root and the custom local OASIS
  image tag.
- **Lines 21--29:** Verify that the current Windows user can access Docker
  before starting a long build; provide a clear error if Docker Desktop is not
  running.
- **Lines 31--35:** Build the `final` Dockerfile target using Docker cache,
  then start the project's Compose stack in detached mode.
- **Lines 36--45:** Print the local Setup and GUI v2 URLs and provide the
  normal Python script entry point.

## `pyproject.toml` and `uv.lock`: package and GUI dependency registration

- **`pyproject.toml`, lines 21--26:** Register the five NOMAD plugin entry
  points: CAMELS API bridge, Measurement schema, Measurement Action, Setup
  Dashboard, and Monitor Dashboard. NOMAD discovers these registrations when
  the distribution is installed.
- **`pyproject.toml`, line 31:** Add `nomad-gui` to the `plugins` optional
  dependency group. The final Docker image needs this package as the Python
  location into which the locally built GUI v2 static assets are copied.
- **`uv.lock`, changes near lines 3508--3545:** Lock the corresponding
  `nomad-gui` package version and its transitive metadata, ensuring that
  `uv sync --extra plugins` installs the same GUI package reproducibly.

## `.vscode/settings.json`: local editor configuration

**Lines 1--3** add a VS Code Python extension setting that selects the system
Python environment manager. This is a local developer convenience setting; it
does not affect the Docker image, OASIS deployment, CAMELS API, or runtime
remote-control functionality. It may be removed if repository policy avoids
committing editor-specific configuration.

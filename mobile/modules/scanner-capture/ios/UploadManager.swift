import Foundation

/// Uploads capture packages to GCS resumable-session URIs with a *background* URLSession.
///
/// - One `PUT` per file for files ≤ `chunkSize` (uploaded straight from the package file).
/// - Larger files are sent as `Content-Range` chunks of `chunkSize` (a multiple of 256 KiB, as GCS
///   requires), each from a temporary slice file because background sessions only upload from files.
/// - After an error / relaunch the committed offset is queried with `PUT` + `Content-Range: bytes */SIZE`
///   (308 + `Range: bytes=0-N` → resume at N+1) and only the remainder is sent.
/// - State is persisted to `Application Support/Scanner/uploads.json` after every change, so the job
///   survives the app being killed; the background session keeps transferring meanwhile.
///
/// All state lives on `q`; URLSession delegate callbacks are delivered on `q` too.
final class UploadManager: NSObject, URLSessionTaskDelegate, URLSessionDataDelegate {
  static let shared = UploadManager()
  static let sessionIdentifier = "xyz.liminal.scanner.upload"
  static let chunkSize: Int64 = 64 * 1024 * 1024
  static let maxInFlight = 4

  struct FileState: Codable {
    var path: String
    var url: String
    var size: Int64
    var committed: Int64 = 0
    var status: String = "pending"  // pending | uploading | done | expired | failed
    var needsProbe = false
    var attempts = 0
    var error: String?
    var notBefore: Date?  // backoff after transient failures
  }

  struct Job: Codable {
    var scanId: String
    var wifiOnly: Bool
    var files: [String: FileState]
    var state: String  // uploading | done | needsUrls | error | paused
    var error: String?
    var createdAt: Date = Date()
  }

  private struct TaskInfo: Codable {
    var scanId: String
    var path: String
    var offset: Int64
    var length: Int64
    var slice: String?
  }

  private let q = DispatchQueue(label: "xyz.liminal.scanner.upload")
  private var jobs: [String: Job] = [:]
  private var inflightSent: [Int: Int64] = [:]       // taskIdentifier -> bytes sent
  private var liveTasks: [Int: TaskInfo] = [:]       // taskIdentifier -> info
  private var probing = Set<String>()                 // "scan|path"
  private var lastProgressEmit: [String: TimeInterval] = [:]
  private var reconciled = false
  private var retryScheduled = false

  var onProgress: (([String: Any]) -> Void)?
  var onState: (([String: Any]) -> Void)?
  var backgroundCompletionHandler: (() -> Void)?

  private lazy var delegateQueue: OperationQueue = {
    let oq = OperationQueue()
    oq.maxConcurrentOperationCount = 1
    oq.underlyingQueue = q
    return oq
  }()

  private(set) lazy var session: URLSession = {
    let c = URLSessionConfiguration.background(withIdentifier: Self.sessionIdentifier)
    c.isDiscretionary = false
    c.sessionSendsLaunchEvents = true
    c.httpMaximumConnectionsPerHost = Self.maxInFlight
    c.timeoutIntervalForRequest = 120
    c.timeoutIntervalForResource = 7 * 24 * 3600
    return URLSession(configuration: c, delegate: self, delegateQueue: delegateQueue)
  }()

  private lazy var probeSession: URLSession = {
    let c = URLSessionConfiguration.ephemeral
    c.timeoutIntervalForRequest = 30
    return URLSession(configuration: c)
  }()

  private var stateURL: URL { ScanStore.supportRoot.appendingPathComponent("uploads.json") }
  private var slicesDir: URL {
    FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("upload-slices")
  }

  override private init() {
    super.init()
    q.sync { load() }
  }

  // MARK: - Public API (any thread)

  /// Re-attaches to the background session and reconciles persisted state with live tasks.
  func activate() {
    q.async { [self] in
      _ = session
      session.getAllTasks { tasks in
        self.q.async {
          self.liveTasks.removeAll()
          for t in tasks {
            if let info = self.info(of: t), t.state == .running || t.state == .suspended {
              self.liveTasks[t.taskIdentifier] = info
            }
          }
          let live = Set(self.liveTasks.values.map { "\($0.scanId)|\($0.path)" })
          for (sid, var job) in self.jobs {
            for (p, var f) in job.files where f.status == "uploading" && !live.contains("\(sid)|\(p)") {
              f.status = "pending"; f.needsProbe = true
              job.files[p] = f
            }
            self.jobs[sid] = job
          }
          self.reconciled = true
          self.save()
          self.pump()
        }
      }
    }
  }

  func start(scanId: String, uploads: [(String, String)], wifiOnly: Bool) throws {
    let dir = ScanStore.packageDir(scanId)
    var files: [String: FileState] = [:]
    for (path, url) in uploads {
      let fileURL = dir.appendingPathComponent(path)
      let attrs = try FileManager.default.attributesOfItem(atPath: fileURL.path)
      let size = (attrs[.size] as? NSNumber)?.int64Value ?? 0
      files[path] = FileState(path: path, url: url, size: size)
    }
    q.async { [self] in
      if var existing = jobs[scanId] {
        // Merge: keep progress for unchanged URLs.
        for (p, f) in files where existing.files[p]?.url != f.url { existing.files[p] = f }
        existing.wifiOnly = wifiOnly
        existing.state = "uploading"; existing.error = nil
        jobs[scanId] = existing
      } else {
        jobs[scanId] = Job(scanId: scanId, wifiOnly: wifiOnly, files: files, state: "uploading")
      }
      save()
      emitState(scanId)
      if reconciled { pump() } else { activate() }
    }
  }

  /// Fresh session URIs (from `POST /scans/{id}/uploads`) for expired files.
  func updateUrls(scanId: String, uploads: [(String, String)]) {
    q.async { [self] in
      guard var job = jobs[scanId] else { return }
      for (p, u) in uploads {
        guard var f = job.files[p] else { continue }
        if f.status == "done" { continue }
        f.url = u; f.committed = 0; f.status = "pending"; f.needsProbe = false; f.error = nil; f.attempts = 0
        f.notBefore = nil
        job.files[p] = f
      }
      job.state = "uploading"; job.error = nil
      jobs[scanId] = job
      save(); emitState(scanId); pump()
    }
  }

  func setWifiOnly(_ on: Bool) {
    q.async { [self] in
      for (k, var j) in jobs where j.wifiOnly != on { j.wifiOnly = on; jobs[k] = j }
      save()
      // Restart in-flight tasks so the new cellular policy applies.
      session.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
    }
  }

  func cancel(scanId: String) {
    q.async { [self] in
      jobs[scanId] = nil
      save()
      session.getAllTasks { tasks in
        for t in tasks where self.info(of: t)?.scanId == scanId { t.cancel() }
      }
      try? FileManager.default.removeItem(at: slicesDir.appendingPathComponent(scanId))
    }
  }

  func state(scanId: String) -> [String: Any]? {
    q.sync { jobs[scanId].map { snapshot($0) } }
  }

  func allStates() -> [[String: Any]] {
    q.sync { jobs.values.map { snapshot($0) } }
  }

  /// Forget a finished job (after `/complete` succeeded).
  func forget(scanId: String) {
    q.async { [self] in jobs[scanId] = nil; save() }
  }

  // MARK: - Scheduling (q)

  private func pump() {
    dispatchPrecondition(condition: .onQueue(q))
    var inFlight = liveTasks.count + probing.count
    for sid in jobs.keys.sorted(by: { jobs[$0]!.createdAt < jobs[$1]!.createdAt }) {
      guard let job = jobs[sid], job.state == "uploading" else { continue }
      // Small files first so the scan list / sidecars land early; big videos last.
      let now = Date()
      let pending = job.files.values
        .filter { $0.status == "pending" && !probing.contains("\(sid)|\($0.path)") }
        .sorted { $0.size < $1.size }
      if pending.contains(where: { ($0.notBefore ?? .distantPast) > now }) { scheduleRetry() }
      for f in pending where (f.notBefore ?? .distantPast) <= now {
        if inFlight >= Self.maxInFlight { return }
        inFlight += 1
        if f.needsProbe { probe(sid, f.path) } else { send(sid, f.path) }
      }
      checkDone(sid)
    }
  }

  private func send(_ sid: String, _ path: String) {
    guard var job = jobs[sid], var f = job.files[path] else { return }
    let fileURL = ScanStore.packageDir(sid).appendingPathComponent(path)
    guard FileManager.default.fileExists(atPath: fileURL.path), let url = URL(string: f.url) else {
      f.status = "failed"; f.error = "missing file or url"
      job.files[path] = f
      job.state = "error"; job.error = "\(path): missing file or url"; jobs[sid] = job
      save(); emitState(sid); return
    }
    let offset = f.committed
    let remaining = f.size - offset
    var req = URLRequest(url: url)
    req.httpMethod = "PUT"
    req.allowsCellularAccess = !job.wifiOnly
    req.allowsExpensiveNetworkAccess = !job.wifiOnly
    req.allowsConstrainedNetworkAccess = !job.wifiOnly
    req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
    var source = fileURL
    var length = remaining
    var slice: String?
    if f.size == 0 {
      req.setValue("bytes */0", forHTTPHeaderField: "Content-Range")
    } else if offset == 0 && f.size <= Self.chunkSize {
      // Whole file in one request, straight from the package (no copy).
    } else {
      length = min(Self.chunkSize, remaining)
      do {
        let s = try makeSlice(sid: sid, path: path, from: fileURL, offset: offset, length: length)
        source = s; slice = s.path
      } catch {
        f.status = "pending"; f.error = "slice: \(error.localizedDescription)"
        f.attempts += 1; f.notBefore = Self.backoff(f.attempts)
        job.files[path] = f; jobs[sid] = job
        scheduleRetry(); return
      }
      req.setValue("bytes \(offset)-\(offset + length - 1)/\(f.size)", forHTTPHeaderField: "Content-Range")
    }
    let task = session.uploadTask(with: req, fromFile: source)
    let info = TaskInfo(scanId: sid, path: path, offset: offset, length: length, slice: slice)
    task.taskDescription = (try? String(data: JSONEncoder().encode(info), encoding: .utf8)) ?? nil
    task.countOfBytesClientExpectsToSend = length
    task.countOfBytesClientExpectsToReceive = 1024
    liveTasks[task.taskIdentifier] = info
    f.status = "uploading"; f.error = nil
    job.files[path] = f; jobs[sid] = job
    save()
    task.resume()
  }

  private func makeSlice(sid: String, path: String, from src: URL, offset: Int64, length: Int64) throws -> URL {
    let dir = slicesDir.appendingPathComponent(sid)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let name = path.replacingOccurrences(of: "/", with: "__") + ".\(offset).part"
    let dst = dir.appendingPathComponent(name)
    try? FileManager.default.removeItem(at: dst)
    FileManager.default.createFile(atPath: dst.path, contents: nil,
                                   attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
    let rh = try FileHandle(forReadingFrom: src)
    let wh = try FileHandle(forWritingTo: dst)
    defer { try? rh.close(); try? wh.close() }
    try rh.seek(toOffset: UInt64(offset))
    var left = length
    while left > 0 {
      let n = Int(min(left, 4 << 20))
      let d: Data? = try autoreleasepool { try rh.read(upToCount: n) }
      guard let d, !d.isEmpty else { throw NSError(domain: "UploadManager", code: 1, userInfo: [NSLocalizedDescriptionKey: "short read"]) }
      try wh.write(contentsOf: d)
      left -= Int64(d.count)
    }
    return dst
  }

  /// Queries the committed offset for a resumable session.
  private func probe(_ sid: String, _ path: String) {
    guard let f = jobs[sid]?.files[path], let url = URL(string: f.url) else { return }
    let key = "\(sid)|\(path)"
    probing.insert(key)
    var req = URLRequest(url: url)
    req.httpMethod = "PUT"
    req.setValue("bytes */\(f.size)", forHTTPHeaderField: "Content-Range")
    req.setValue("0", forHTTPHeaderField: "Content-Length")
    req.httpBody = Data()
    probeSession.dataTask(with: req) { [weak self] _, resp, err in
      guard let self else { return }
      self.q.async {
        self.probing.remove(key)
        guard var job = self.jobs[sid], var f = job.files[path] else { return }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        switch Self.classify(status: code, error: err) {
        case .done:
          f.status = "done"; f.committed = f.size; f.needsProbe = false
        case .incomplete:
          f.committed = Self.committedOffset(from: resp as? HTTPURLResponse)
          f.status = "pending"; f.needsProbe = false
        case .expired:
          f.status = "expired"; f.needsProbe = false
        case .retry:
          f.attempts += 1; f.error = err?.localizedDescription ?? "probe HTTP \(code)"
          f.notBefore = Self.backoff(f.attempts)
        case .fatal:
          f.status = "failed"; f.error = "probe HTTP \(code)"
        }
        job.files[path] = f; self.jobs[sid] = job
        self.afterFileChange(sid)
      }
    }.resume()
  }

  enum Outcome { case done, incomplete, expired, retry, fatal }

  /// Maps a GCS resumable-upload response to an action.
  static func classify(status: Int, error: Error?) -> Outcome {
    if error != nil { return .retry }  // network errors and cancellations: probe + resume
    switch status {
    case 200, 201: return .done
    case 308: return .incomplete
    case 401, 403, 404, 410: return .expired
    case 408, 429, 500...599, 0: return .retry
    default: return .fatal
    }
  }

  static func backoff(_ attempts: Int) -> Date {
    Date().addingTimeInterval(min(300, 5 * pow(2, Double(min(attempts, 6)))))
  }

  /// `Range: bytes=0-N` → N+1; absent → 0.
  static func committedOffset(from resp: HTTPURLResponse?) -> Int64 {
    guard let r = resp?.value(forHTTPHeaderField: "Range"),
          let dash = r.lastIndex(of: "-"), let n = Int64(r[r.index(after: dash)...]) else { return 0 }
    return n + 1
  }

  private func afterFileChange(_ sid: String) {
    guard var job = jobs[sid] else { return }
    let expired = job.files.values.filter { $0.status == "expired" }.map(\.path)
    let failed = job.files.values.filter { $0.status == "failed" }
    if !expired.isEmpty && job.files.values.allSatisfy({ $0.status != "uploading" }) {
      job.state = "needsUrls"
    } else if !failed.isEmpty && job.files.values.allSatisfy({ $0.status != "uploading" && $0.status != "pending" }) {
      job.state = "error"; job.error = failed.map { "\($0.path): \($0.error ?? "failed")" }.joined(separator: "; ")
    }
    jobs[sid] = job
    save()
    emitProgress(sid, force: true)
    checkDone(sid)
    emitState(sid)
    pump()
  }

  private func checkDone(_ sid: String) {
    guard var job = jobs[sid], job.state == "uploading" else { return }
    if job.files.values.allSatisfy({ $0.status == "done" }) {
      job.state = "done"; jobs[sid] = job; save()
      try? FileManager.default.removeItem(at: slicesDir.appendingPathComponent(sid))
      emitState(sid)
    }
  }

  private func scheduleRetry() {
    guard !retryScheduled else { return }
    retryScheduled = true
    q.asyncAfter(deadline: .now() + 5) { [self] in
      retryScheduled = false
      pump()
    }
  }

  // MARK: - URLSession delegate (on q)

  func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                  totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
    inflightSent[task.taskIdentifier] = totalBytesSent
    if let sid = info(of: task)?.scanId { emitProgress(sid, force: false) }
  }

  func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
    inflightSent[task.taskIdentifier] = nil
    liveTasks[task.taskIdentifier] = nil
    guard let info = info(of: task) else { return }
    if let s = info.slice { try? FileManager.default.removeItem(atPath: s) }
    guard var job = jobs[info.scanId], var f = job.files[info.path] else { return }
    let resp = task.response as? HTTPURLResponse
    let code = resp?.statusCode ?? 0
    switch Self.classify(status: code, error: error) {
    case .done:
      f.status = "done"; f.committed = f.size; f.error = nil
    case .incomplete:
      // Chunk accepted; GCS tells us how much it has.
      f.committed = Self.committedOffset(from: resp)
      f.status = "pending"; f.attempts = 0; f.notBefore = nil
    case .expired:
      f.status = "expired"
    case .retry:
      f.status = "pending"; f.needsProbe = true; f.attempts += 1
      f.error = error?.localizedDescription ?? "HTTP \(code)"
      f.notBefore = Self.backoff(f.attempts)
    case .fatal:
      f.status = "failed"; f.error = "HTTP \(code)"
    }
    job.files[info.path] = f
    jobs[info.scanId] = job
    afterFileChange(info.scanId)
  }

  func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
    q.async { [self] in
      let h = backgroundCompletionHandler
      backgroundCompletionHandler = nil
      DispatchQueue.main.async { h?() }
    }
  }

  // MARK: - Helpers (q)

  private func info(of task: URLSessionTask) -> TaskInfo? {
    guard let d = task.taskDescription?.data(using: .utf8) else { return nil }
    return try? JSONDecoder().decode(TaskInfo.self, from: d)
  }

  private func progress(_ job: Job) -> (Int64, Int64, Int, Int) {
    var sent: Int64 = 0, total: Int64 = 0, done = 0
    var inflightByPath: [String: Int64] = [:]
    for (tid, info) in liveTasks where info.scanId == job.scanId {
      inflightByPath[info.path, default: 0] += inflightSent[tid] ?? 0
    }
    for f in job.files.values {
      total += f.size
      if f.status == "done" { sent += f.size; done += 1 } else {
        sent += min(f.size, f.committed + (inflightByPath[f.path] ?? 0))
      }
    }
    return (sent, total, done, job.files.count)
  }

  private func snapshot(_ job: Job) -> [String: Any] {
    let (sent, total, done, count) = progress(job)
    var d: [String: Any] = [
      "scanId": job.scanId, "state": job.state, "wifiOnly": job.wifiOnly,
      "sentBytes": sent, "totalBytes": total, "filesDone": done, "filesTotal": count,
      "files": job.files.values.sorted { $0.path < $1.path }.map {
        ["path": $0.path, "size": $0.size, "committed": $0.committed, "status": $0.status, "error": $0.error as Any]
      },
      "expiredPaths": job.files.values.filter { $0.status == "expired" }.map(\.path).sorted(),
    ]
    if let e = job.error { d["error"] = e }
    return d
  }

  private func emitProgress(_ sid: String, force: Bool) {
    guard let job = jobs[sid] else { return }
    let now = Date().timeIntervalSince1970
    if !force, now - (lastProgressEmit[sid] ?? 0) < 0.5 { return }
    lastProgressEmit[sid] = now
    let (sent, total, done, count) = progress(job)
    onProgress?(["scanId": sid, "sentBytes": sent, "totalBytes": total, "filesDone": done, "filesTotal": count])
  }

  private func emitState(_ sid: String) {
    guard let job = jobs[sid] else { return }
    onState?(snapshot(job))
  }

  private func load() {
    guard let d = try? Data(contentsOf: stateURL) else { return }
    jobs = (try? JSONDecoder().decode([String: Job].self, from: d)) ?? [:]
  }

  private func save() {
    do {
      try FileManager.default.createDirectory(at: stateURL.deletingLastPathComponent(), withIntermediateDirectories: true)
      try JSONEncoder().encode(jobs).write(to: stateURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    } catch {
      NSLog("[ScannerCapture] failed to persist upload state: \(error)")
    }
  }
}

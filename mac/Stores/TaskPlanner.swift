import Foundation
import Observation

/// One instance per form: an override and an approval apply only to that submission.
@MainActor @Observable
final class TaskPlanner {
    var selected: String?
    var plan: ComputePlan?
    var groups: [SweepGroup] = []
    var allCompleted = false
    var previewEngine: String?
    var previewKind = "gen"
    var taskSummary = ""
    var loading = false
    var error: String?
    var submitting = false
    var confirmation: ComputePlan?
    var confirmationDevice: String?
    @ObservationIgnored private var decision: CheckedContinuation<Bool, Never>?
    private var revision = 0

    func clearPreview() { revision += 1; plan = nil; groups = []; allCompleted = false; loading = false; error = nil }
    func refresh<B: Encodable>(api: APIClient, input: B, sweep: Bool = false) async {
        if let request = input as? SweepRequest, !request.ready { reset(); return }
        previewKind = (input as? PlanInput)?.kind ?? "train"
        previewEngine = (input as? PlanInput)?.engine ?? (input as? PlanInput)?.engines?.first ?? (input as? PlanInput)?.parts?.first?.engine ?? (input as? SweepRequest)?.engines.first
        revision += 1
        let token = revision
        loading = true
        error = nil
        defer { if revision == token { loading = false } }
        do {
            let body = try Self.body(input, compute: selected)
            let data = try await api.data(api.request(sweep ? "/train/sweep/plan" : "/plan", method: "POST", body: body), retry503: false)
            try Task.checkCancellation()
            guard token == revision else { return }
            if sweep {
                let value = try JSONDecoder().decode(SweepPlan.self, from: data)
                plan = value.plan; groups = value.groups; allCompleted = value.plan == nil
            } else {
                plan = try JSONDecoder().decode(ComputePlan.self, from: data)
                groups = []; allCompleted = false
            }
        } catch is CancellationError { }
        catch { if !Task.isCancelled && token == revision { plan = nil; allCompleted = false; self.error = error.localizedDescription } }
    }
    func submit<T: Decodable, B: Encodable>(api: APIClient, path: String, input: B) async throws -> T {
        guard !submitting else { throw CancellationError() }
        submitting = true
        defer { submitting = false; selected = nil; confirmation = nil; confirmationDevice = nil; decision = nil }
        let inputData = try JSONEncoder().encode(input)
        var device = selected
        var approval: PlanCandidate?
        while true {
            try Task.checkCancellation()
            let data = try Self.body(data: inputData, compute: device, approval: approval)
            do {
                let response = try await api.data(api.request(path, method: "POST", body: data), retry503: false)
                return try JSONDecoder().decode(T.self, from: response)
            } catch let failure as APIError where failure.code == "confirm_required" {
                guard let quote = failure.plan else { throw failure }
                confirmation = quote
                confirmationDevice = quote.chosen
                let accepted = await withTaskCancellationHandler {
                    await withCheckedContinuation { continuation in
                        if Task.isCancelled { continuation.resume(returning: false) }
                        else { decision = continuation }
                    }
                } onCancel: { Task { @MainActor in self.resolve(false) } }
                guard accepted, let chosen = quote.candidates.first(where: { $0.device == confirmationDevice }), chosen.available else { throw CancellationError() }
                device = chosen.device
                approval = chosen
                confirmation = nil
            } catch let failure as APIError where failure.code == "compute_unavailable" {
                plan = failure.plan
                let message = failure.plan?.selected?.unavailableReason ?? computeFailure(failure.message)
                error = message
                throw APIError(status: failure.status, message: message, code: failure.code, plan: failure.plan)
            }
        }
    }
    static func previewID<B: Encodable>(_ input: B) -> String {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        return String(decoding: (try? encoder.encode(input)) ?? Data(), as: UTF8.self)
    }
    func reset() {
        revision += 1
        resolve(false)
        selected = nil; plan = nil; groups = []; allCompleted = false; error = nil; loading = false
    }
    func resolve(_ accepted: Bool) { let value = decision; decision = nil; value?.resume(returning: accepted) }
    static func body<B: Encodable>(_ input: B, compute: String?) throws -> Data {
        try body(data: JSONEncoder().encode(input), compute: compute, approval: nil)
    }
    static func body(data: Data, compute: String?, approval: PlanCandidate?) throws -> Data {
        var fields = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        if let compute { fields["compute"] = compute }
        if let approval {
            fields["approved"] = true
            fields["approved_device"] = approval.device
            fields["approved_units"] = approval.units
        }
        return try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
    }
}

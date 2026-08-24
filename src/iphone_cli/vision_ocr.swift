import Foundation
import ImageIO
import Vision

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: vision_ocr.swift IMAGE\n".utf8))
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard
    let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
    let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
else {
    FileHandle.standardError.write(Data("could not decode image\n".utf8))
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

do {
    try VNImageRequestHandler(cgImage: image).perform([request])
} catch {
    FileHandle.standardError.write(Data("Vision request failed: \(error)\n".utf8))
    exit(1)
}

let observations = (request.results ?? []).sorted { left, right in
    let verticalDistance = abs(left.boundingBox.midY - right.boundingBox.midY)
    if verticalDistance > 0.02 {
        return left.boundingBox.midY > right.boundingBox.midY
    }
    return left.boundingBox.minX < right.boundingBox.minX
}

let lines = observations.compactMap { $0.topCandidates(1).first?.string }
print(lines.joined(separator: "\n"))

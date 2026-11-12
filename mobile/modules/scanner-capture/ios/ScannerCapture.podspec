Pod::Spec.new do |s|
  s.name           = 'ScannerCapture'
  s.version        = '0.1.0'
  s.summary        = 'ARKit LiDAR capture + GCS background upload for Scanner'
  s.description    = 'ARKit recording (HEVC, poses, depth, IMU, stills, mesh), manifest + CRC32C, background resumable upload.'
  s.author         = 'Liminal'
  s.homepage       = 'https://liminal.xyz'
  s.license        = { :type => 'Proprietary' }
  s.platforms      = { :ios => '18.0' }
  s.source         = { git: '' }
  s.static_framework = true
  s.swift_version  = '5.9'

  s.dependency 'ExpoModulesCore'
  s.frameworks = 'ARKit', 'AVFoundation', 'CoreMotion', 'CoreImage', 'SceneKit', 'VideoToolbox'
  s.library = 'z'

  s.pod_target_xcconfig = {
    'DEFINES_MODULE' => 'YES',
    'SWIFT_COMPILATION_MODE' => 'wholemodule',
  }

  s.source_files = "**/*.{h,m,mm,swift}"
end

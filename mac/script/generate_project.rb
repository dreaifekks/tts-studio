require 'xcodeproj'
root = File.expand_path('..', __dir__)
project = Xcodeproj::Project.new(File.join(root, 'TTSStudio.xcodeproj'))
project.root_object.development_region = 'zh-Hans'
project.root_object.known_regions = ['zh-Hans', 'Base']
target = project.new_target(:application, 'TTSStudio', :osx, '27.0')
%w[App Models Services Stores Views Support].each do |dir|
  group = project.main_group.new_group(dir, dir)
  Dir.glob(File.join(root, dir, '*')).sort.each do |file|
    ref = group.new_file(File.basename(file))
    target.source_build_phase.add_file_reference(ref) if file.end_with?('.swift')
  end
end
project.build_configurations.each do |c|
  c.build_settings['MACOSX_DEPLOYMENT_TARGET'] = '27.0'
  c.build_settings['SWIFT_VERSION'] = '6.0'
end
target.build_configurations.each do |c|
  c.build_settings.merge!({
    'PRODUCT_BUNDLE_IDENTIFIER'=>'com.dreaife.tts-studio',
    'PRODUCT_NAME'=>'TTSStudio',
    'TTS_STUDIO_REVISION'=>'development',
    'TTS_STUDIO_VERSION'=>'0.0.0',
    'INFOPLIST_FILE'=>'App/Info.plist',
    'CODE_SIGN_ENTITLEMENTS'=>'App/TTSStudio.entitlements',
    'CODE_SIGN_IDENTITY'=>'-',
    'CODE_SIGN_STYLE'=>'Manual',
    'SWIFT_VERSION'=>'6.0',
    'SWIFT_STRICT_CONCURRENCY'=>'complete',
    'ENABLE_HARDENED_RUNTIME'=>'YES',
    'COMBINE_HIDPI_IMAGES'=>'YES',
    'ASSETCATALOG_COMPILER_GENERATE_SWIFT_ASSET_SYMBOL_EXTENSIONS'=>'NO'
  })
end
stamp = target.new_shell_script_build_phase('Git version and updater')
stamp.shell_script = '/bin/bash "$SRCROOT/script/stamp-version.sh"'
stamp.input_paths = ['$(TARGET_BUILD_DIR)/$(INFOPLIST_PATH)']
stamp.always_out_of_date = '1'
unit = project.new_target(:unit_test_bundle, 'TTSStudioTests', :osx, '27.0')
unit.add_dependency(target)
group = project.main_group.new_group('Tests', 'Tests')
Dir.glob(File.join(root, 'Tests', '*.swift')).sort.each { |file| unit.source_build_phase.add_file_reference(group.new_file(File.basename(file))) }
probe = group.new_file('TerminalHandoff.command')
unit.resources_build_phase.add_file_reference(probe)
unit.build_configurations.each do |c|
  c.build_settings.merge!({'PRODUCT_BUNDLE_IDENTIFIER'=>'com.dreaife.tts-studio.tests','GENERATE_INFOPLIST_FILE'=>'YES','SWIFT_VERSION'=>'6.0','TEST_HOST'=>'$(BUILT_PRODUCTS_DIR)/TTSStudio.app/Contents/MacOS/TTSStudio','BUNDLE_LOADER'=>'$(TEST_HOST)','CODE_SIGN_IDENTITY'=>'-'})
end
project.save
scheme = Xcodeproj::XCScheme.new
scheme.add_build_target(target)
scheme.set_launch_target(target)
scheme.add_test_target(unit)
scheme.test_action.should_use_launch_scheme_args_env = false
scheme.test_action.environment_variables = Xcodeproj::XCScheme::EnvironmentVariables.new([{key: 'TTS_LIVE_ACCEPTANCE', value: '$(TTS_LIVE_ACCEPTANCE)', enabled: true}])
scheme.save_as(project.path, 'TTSStudio', true)

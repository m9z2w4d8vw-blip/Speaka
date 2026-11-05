"""Portable runtime regressions; real iOS playback is a separate device test."""
import os, pathlib, shutil, subprocess, tempfile, unittest
P=pathlib.Path(__file__).resolve().parents[1]
class V88Runtime(unittest.TestCase):
 def test_shared_ring_control_and_continuous_decode(self):
  cc=shutil.which('cc')
  self.assertIsNotNone(cc)
  with tempfile.TemporaryDirectory() as td:
   exe=str(pathlib.Path(td)/'v88')
   src=['tests/v88_runtime.c','shared/AudioRing.c','tweak/StreamControl.c',
        'audiohelper/SBCDecoder.c',
        'audiohelper/vendor_sbc/sbc.c','audiohelper/vendor_sbc/bits.c']
   flags=['-std=gnu11','-O2','-Wall','-Wextra','-Werror','-pthread','-I'+str(P),'-I'+str(P/'audiohelper/vendor_sbc')]
   if os.environ.get('BTPS_SANITIZE'): flags+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
   objects=[]
   for i,file in enumerate(src):
    obj=str(pathlib.Path(td)/('%d.o'%i));objects.append(obj)
    # Upstream fixed-point SBC deliberately shifts signed negatives. Check
    # upstream memory with ASan; use UBSan on our new queue/control/adapter.
    opts=[f.replace('-fsanitize=address,undefined','-fsanitize=address') for f in flags if f!='-Werror'] if '/vendor_sbc/' in file else flags
    r=subprocess.run([cc,*opts,'-c',str(P/file),'-o',obj],capture_output=True,text=True)
    self.assertEqual(r.returncode,0,r.stderr)
   r=subprocess.run([cc,*flags,*objects,'-o',exe],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
   env=dict(os.environ)
   if os.environ.get('BTPS_SANITIZE'):
    env['ASAN_OPTIONS']='detect_leaks=0' # LSan cannot inspect /proc under this runner
    env['UBSAN_OPTIONS']='halt_on_error=1'
   out=str(pathlib.Path(td)/'decoded.pcm')
   r=subprocess.run([exe,str(P/'tests/fixtures/v75_actual_captured_258_frames.sbc'),out],capture_output=True,text=True,timeout=30,env=env)
   self.assertEqual(r.returncode,0,r.stdout+r.stderr)
   print(r.stdout,end='')
   # The live decoder's output against ffmpeg's own decode of the same recording, so a wrong libsbc can't pass
   # just by matching itself (moved from test_v76_decoder.py with the v7.6 adapter): no clipping, a similar
   # level, and the same waveform once past the start.
   import array, math, sys, wave
   x=array.array('h');x.frombytes(pathlib.Path(out).read_bytes())
   self.assertEqual(len(x),33024*2)
   with wave.open(str(P/'tests/fixtures/v75_actual_ffmpeg_reference.wav')) as wav:
    self.assertEqual((wav.getnchannels(),wav.getframerate(),wav.getnframes()),(2,44100,33024))
    ref=array.array('h');ref.frombytes(wav.readframes(wav.getnframes()))
   if sys.byteorder!='little':ref.byteswap()
   ref_rms=math.sqrt(sum(y*y for y in ref)/len(ref))/32768
   rms=math.sqrt(sum(y*y for y in x)/len(x))/32768
   clipped=sum(abs(y)>=32000 for y in x)/len(x)
   self.assertLess(clipped,0.01,'Decoded output clipped, fail rather than play broken audio')
   self.assertGreater(rms,ref_rms*.65)
   self.assertLess(rms,ref_rms*1.6)
   for ch in (0,1):
    a=x[4096*2+ch::2];b=ref[4096*2+ch::2]
    dot=sum(float(u)*v for u,v in zip(a,b));sa=sum(float(u)*u for u in a);sb=sum(float(v)*v for v in b)
    corr=dot/(math.sqrt(sa*sb)+1e-30)
    self.assertGreater(abs(corr),0.80,'Decoded waveform does not resemble reference channel %d: corr %g'%(ch,corr))
 def test_receive_thread_has_no_io_allocation_or_tls(self):
  s=(P/'tweak/AudioRelay.c').read_text().split('void AudioRelayForward(',1)[1].split('static void drain_logs',1)[0]
  for term in ('SpeakaLogLine','malloc(', 'calloc(', 'send(', 'recv(', 'write(', 'pthread_', 'dispatch_', 'safe_peek', '__thread'):
   self.assertNotIn(term,s)
  queue=(P/'shared/AudioRing.c').read_text()
  for term in ('malloc(', 'pthread_', 'usleep(', 'write(', 'syscall', 'while('): self.assertNotIn(term,queue)
 def test_activation_is_one_shot_and_callback_is_table_swap(self):
  s=(P/'tweak/Hooks.xm').read_text()
  self.assertIn('if(g_sink_armed && uuid_ok) g_relay_ready=AudioRelayPrepare();',s)
  block=s.split('static int install_media_callback(',1)[1].split('/* v8.3 one-shot:',1)[0]
  self.assertNotIn('MSHookFunction',block)
  self.assertIn('ptrauth_sign_unauthenticated(raw,ptrauth_key_function_pointer,0)',block)
  self.assertIn('g_media_callbacks[22]',block)
 def test_tx_packet_captured_before_native_ownership_transfer(self):
  s=(P/'tweak/Hooks.xm').read_text().split('static uint64_t hook_transmit_signal(',1)[1].split('static void install_signal_tx(',1)[0]
  self.assertLess(s.index('int control_ok='),s.index('uint64_t ret = g_signal_tx_orig'))
  self.assertIn('if(ret==0u && control_ok)',s)
 def test_launchd_plist(self):
  import plistlib
  p=plistlib.loads((P/'layout/Library/LaunchDaemons/com.parv.speaka.audio.plist').read_bytes())
  self.assertEqual(p['UserName'],'mobile')
  self.assertEqual(p['Sockets']['Stream']['SockNodeName'],'127.0.0.1')
  self.assertEqual(p['ProgramArguments'],['/var/jb/usr/bin/BTPSAudioHelper','--stream-service'])
  self.assertNotIn('KeepAlive',p)
if __name__=='__main__':unittest.main()

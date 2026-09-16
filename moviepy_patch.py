"""
moviepy_patch.py
----------------
Corrige um deadlock real do MoviePy 1.0.3 na LEITURA de mídia.

O problema
==========
Ao abrir um arquivo, o FFMPEG_VideoReader (e o FFMPEG_AudioReader) sobem o
ffmpeg assim:

    popen_params = {"stdout": sp.PIPE,
                    "stderr": sp.PIPE,     # <- pipe que o MoviePy NUNCA lê
                    "stdin": DEVNULL}

O MoviePy lê o stdout (os frames), mas nunca lê o stderr. Um pipe do SO tem
buffer limitado (tipicamente 64 KB no Linux). Se o arquivo de origem fizer o
ffmpeg emitir erro/aviso em volume suficiente — arquivo levemente corrompido,
container estranho, frames quebrados, um MP4 baixado pela metade —, esse buffer
enche. Aí o ffmpeg BLOQUEIA tentando escrever no stderr, e como consequência
para de produzir frames no stdout. Do lado do Python, isso aparece como:

    ffmpeg_reader.py, line 120 in read_frame
        s = self.proc.stdout.read(nbytes)

parado pra sempre. Sem erro, sem timeout, sem log — o processo fica vivo e
aparentemente "renderizando", só que travado no mesmo frame indefinidamente.

É o mesmo defeito que no lado da ESCRITA se resolve com
write_videofile(..., write_logfile=True) — mas o leitor não tem parâmetro
equivalente, então precisa deste patch.

A correção
==========
Sobe, junto com cada ffmpeg de LEITURA de frames, uma thread daemon que fica
consumindo o stderr dele. Assim o buffer nunca enche e o ffmpeg nunca bloqueia,
eliminando o deadlock na raiz. Nada de informação é perdido: o MoviePy já nunca
lia esse stderr. Os avisos que ele de fato usa (ex: "bytes wanted but bytes
read") continuam funcionando, porque vêm da checagem do stdout.

Em vez de reescrever o método initialize() inteiro (que duplicaria a montagem
do comando do ffmpeg e poderia divergir da lib), o patch troca só o objeto
`sp` (o módulo subprocess) que esses módulos usam, por um proxy que repassa
tudo e só ajusta o stderr na hora de criar o processo. Menos superfície,
menos chance de quebrar.

Uso: `import moviepy_patch` UMA vez, antes de abrir qualquer mídia.
"""
import subprocess as _subprocess
import threading as _threading
import os as _os


def _drenar(fd_copia):
    """
    Consome o pipe até o fim, jogando fora, lendo por um descritor DUPLICADO.

    Duplicar é essencial: se a thread lesse direto de `proc.stderr`, ela
    disputaria o lock interno daquele mesmo objeto de I/O com o close() do
    MoviePy, o que chega a derrubar o interpretador com
    "Fatal Python error: _enter_buffered_busy: could not acquire lock ...
    at interpreter shutdown, possibly due to daemon threads".

    Lendo por uma cópia do descritor com os.read (I/O cru, sem buffer nem
    lock compartilhado), a drenagem esvazia o mesmo pipe sem nunca tocar no
    objeto que o MoviePy manipula.
    """
    try:
        while True:
            if not _os.read(fd_copia, 8192):
                break
    except OSError:
        pass
    finally:
        try:
            _os.close(fd_copia)
        except OSError:
            pass


class _SubprocessSemStderrPipe:
    """
    Proxy do módulo subprocess que, nos processos de LEITURA DE FRAMES, sobe
    junto uma thread que fica drenando o stderr do ffmpeg.

    Por que drenar em vez de mandar pra DEVNULL: o MoviePy mexe no
    `proc.stderr` depois — o close() do leitor faz `self.proc.stderr.close()`,
    e com DEVNULL o atributo vira None, quebrando com
    "AttributeError: 'NoneType' object has no attribute 'close'". Drenando, o
    `proc.stderr` continua sendo um objeto de verdade e todo o comportamento
    original do MoviePy segue intacto — só que agora o buffer nunca enche,
    que é o que causava o deadlock.

    E por que só nos processos de leitura de frames: o ffmpeg_parse_infos()
    também sobe o ffmpeg com stderr=PIPE, mas ele LÊ esse stderr de propósito
    (é de lá que vêm duração, resolução e fps). Drenar o dele faria os
    metadados chegarem vazios. Como os dois usam popen_params idênticos, a
    distinção é pelo COMANDO: quem streama frames termina com '-' (saída crua
    pro stdout); o parse_infos termina com o nome do arquivo (ou /dev/null,
    no caso de GIF).
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, nome):
        # qualquer outro atributo (PIPE, DEVNULL, CalledProcessError...) passa direto
        return getattr(self._real, nome)

    @staticmethod
    def _e_leitura_de_frames(args):
        try:
            cmd = args[0]
            return isinstance(cmd, (list, tuple)) and len(cmd) > 0 and cmd[-1] == '-'
        except (IndexError, TypeError):
            return False

    def Popen(self, *args, **kwargs):
        proc = self._real.Popen(*args, **kwargs)
        if (kwargs.get('stderr') is self._real.PIPE
                and self._e_leitura_de_frames(args)
                and proc.stderr is not None):
            try:
                fd_copia = _os.dup(proc.stderr.fileno())
            except OSError:
                return proc  # sem drenagem; no pior caso volta ao comportamento original
            _threading.Thread(target=_drenar, args=(fd_copia,), daemon=True).start()
        return proc


def aplicar():
    """Aplica o patch nos leitores de vídeo e áudio. Idempotente."""
    alvos = []

    try:
        from moviepy.video.io import ffmpeg_reader
        alvos.append(ffmpeg_reader)
    except ImportError:
        pass

    try:
        from moviepy.audio.io import readers as audio_readers
        alvos.append(audio_readers)
    except ImportError:
        pass

    aplicados = []
    for modulo in alvos:
        sp_atual = getattr(modulo, 'sp', None)
        if sp_atual is None:
            continue
        if isinstance(sp_atual, _SubprocessSemStderrPipe):
            continue  # já aplicado
        modulo.sp = _SubprocessSemStderrPipe(sp_atual)
        aplicados.append(modulo.__name__)

    return aplicados


_aplicados = aplicar()
if _aplicados:
    print(f"🩹 Patch anti-deadlock do MoviePy aplicado em: {', '.join(_aplicados)}")

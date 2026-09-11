"""
Watchdog de tempo TOTAL pra chamadas de rede (principalmente download de arquivo
binário — imagem/vídeo) que podem travar pra sempre mesmo tendo um `timeout=`
passado pro requests.

Isso não é sobre "esquecer" de passar timeout: o problema é que o `timeout=`
do requests vale por LEITURA individual (cada pedacinho de dado recebido),
não pelo tempo total da chamada. Se o servidor (comum no archive.org e às
vezes no Wikimedia, especialmente vindo de IP de datacenter do GitHub Actions,
que costumam ser throttlados) ficar mandando bytes bem devagar — um pedaço a
cada N segundos, sempre abaixo do timeout — a chamada nunca estoura o timeout
e nunca termina. Fica pendurada indefinidamente, sem erro, sem log, sem nada.

com_watchdog roda a chamada numa thread separada e força um limite de tempo
TOTAL de verdade: se estourar, a função desiste e segue em frente (a thread
"trava" sozinha no limbo, mas isso não bloqueia o processo principal).
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError


def com_watchdog(func, *args, timeout_total=45, label=None, **kwargs):
    """
    Executa func(*args, **kwargs) com um teto de tempo TOTAL (parede, não só
    entre leituras). Devolve o retorno normal de func, ou None se estourar o
    tempo ou se func levantar qualquer exceção — mesmo contrato de "falhou,
    quem chamou decide o fallback" que os try/except ao redor dos downloads
    já usavam antes.
    """
    nome = label or getattr(func, '__name__', 'chamada de rede')
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout_total)
        except FutureTimeoutError:
            print(f"    ⚠️ {nome} travou além de {timeout_total}s — abortando e seguindo sem essa mídia")
            return None
        except Exception as e:
            print(f"    ⚠️ {nome} falhou ({e})")
            return None

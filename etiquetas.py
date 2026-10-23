"""Etiquetas Zebra (10x5 cm, 203 dpi): generación de ZPL e impresión directa por red.
 
Variables de entorno:
  ZEBRA_IP    IP de la impresora (147.120.0.181)
  ZEBRA_PORT  puerto RAW (por defecto 6101)
"""
import os
import socket
 
ZEBRA_IP = os.environ.get("ZEBRA_IP", "147.120.0.181")
ZEBRA_PORT = int(os.environ.get("ZEBRA_PORT", "6101"))
 
 
def _limpiar(texto: str) -> str:
    return str(texto).replace("^", " ").replace("~", " ").strip()
 
 
def generar_zpl(referencia: str, color: str, copias: int = 1) -> str:
    ref = _limpiar(referencia)
    col = _limpiar(color)
    return f"""^XA
^PW799
^LL408
^LH0,0
^CI28
^PQ{max(1, int(copias))}
^FO30,25^A0N,90,90^FB740,1,0,L^FD{ref}^FS
^FO30,135^A0N,60,60^FB740,1,0,L^FD{col}^FS
^FO30,230^BY3^BCN,110,Y,N,N^FD{ref}^FS
^XZ"""
 
 
def imprimir_etiqueta(referencia: str, color: str, copias: int = 1) -> None:
    """Envía la etiqueta a la Zebra. Bloqueante: llamar con asyncio.to_thread."""
    zpl = generar_zpl(referencia, color, copias)
    with socket.create_connection((ZEBRA_IP, ZEBRA_PORT), timeout=10) as s:
        s.sendall(zpl.encode("utf-8"))

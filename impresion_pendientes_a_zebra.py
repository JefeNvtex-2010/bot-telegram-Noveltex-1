#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Feb 21 18:59:09 2025

@author: jmejia
"""

import socket
import os  # <-- Modificado para leer las variables de entorno de Render
import requests
import xml.etree.ElementTree as ET

def AddNextKey(Dict, NewData, CadenaKey):
    Dict[str(CadenaKey)+'_%d' % (len(Dict) + 1)] = NewData

def AddNextKeyCus(Dict, NewData, CadenaKey, Contador):
    Dict[str(CadenaKey)+'_%d' % ( Contador )] = NewData
    
def monthdelta(date, delta):
    m, y = (date.month+delta) % 12, date.year + ((date.month)+delta-1) // 12
    if not m: m = 12
    d = min(date.day, [31,
        29 if y%4==0 and (not y%100==0 or y%400 == 0) else 28,
        31,30,31,30,31,31,30,31,30,31][m-1])
    return date.replace(day=d,month=m, year=y)

import datetime
import pytz
import urllib.request
import json
import time
import logging

def imprimir_zebra(host, port, comando):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        try:
            # Enviar comando ZPL
            sock.send(comando.encode())
            print('comando.encode()', comando.encode())
            sock.close()
            print("✅ Comando enviado correctamente a la impresora Zebra")
        except Exception as e:
            print(f"❌ Error despues de conectar con la impresora: {e}")
            try:
                sock.close()
            except Exception as e:
                print(f"❌ Error al cerrar conexion con la impresora: {e}")
    except Exception as e:
        print(f"❌ Error al conectar con la impresora: {e}")

def imprimir_zebra_alt(host, port, comando):
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            print("📤 Enviando comando a impresora Zebra...")
            sock.sendall(comando.encode('utf-8'))
            print("✅ Comando enviado.")
    except Exception as e:
        print(f"❌ Error al enviar a la impresora: {e}")

def imprimir_zebra_v2(host, port, comando):
    """ Envía el comando ZPL a la impresora Zebra """
    REINTENTOS = 3
    for intento in range(REINTENTOS):
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                sock.sendall(comando.encode())
            logging.info(f"✅ Impresión enviada a {host}:{port}")
            return True
        except Exception as e:
            logging.warning(f"⚠️ Intento {intento+1}: No se pudo conectar a {host}:{port}. Error: {e}")
            time.sleep(2)
    logging.error(f"❌ No se pudo imprimir en {host}:{port} después de {REINTENTOS} intentos")
    return False

def generar_zpl_normal(datos):
    """ Genera una etiqueta ZPL con el texto indicado """
    zpl_template = """^XA
^CI28
^FT78,318^A0B,34,41^FH\^CI28^FDNOVELTEX SAS^FS^CI27
^FT330,365^A0B,28,28^FH\^CI28^FDItem^FS^CI27
^FT390,365^A0B,28,28^FH\^CI28^FDRef.^FS^CI27
^FT449,365^A0B,28,28^FH\^CI28^FDColor^FS^CI27
^FT508,365^A0B,28,28^FH\^CI28^FDMetros^FS^CI27
^FT567,365^A0B,28,28^FH\^CI28^FDOperario^FS^CI27
^FT645,381^A0B,39,41^FH\^CI28^FDPEDIDO^FS^CI27
^FO601,22^GB62,227,4^FS
^FT686,395^A0B,20,20^FB395,1,5,C^FH\^CI28^FDconserve esta etiqueta en caso de\\5C&^FS^CI27
^FT711,395^A0B,20,20^FB395,1,5,C^FH\^CI28^FDdevolución o reclamación\\5C&^FS^CI27
^FT758,275^A0B,23,23^FH\^CI28^FD{fecha}^FS^CI27
^FT268,271^A0B,23,23^FH\^CI28^FD{codigo_rollo_hijo}^FS^CI27
^FT112,287^BQN,2,6
^FDQA,{codigo_rollo_hijo}^FS
^FT330,250^A0B,28,28^FH\^CI28^FD{codigo_tela_hijo}^FS^CI27
^FT390,250^A0B,24,24^FH\^CI28^FD{id_refer}^FS^CI27
^FT449,250^A0B,28,28^FH\^CI28^FD{codigo_color_hijo}^FS^CI27
^FT508,250^A0B,28,28^FH\^CI28^FD{longitud_hijo}^FS^CI27
^FT559,250^A0B,20,20^FH\^CI28^FD{nombre_completo}^FS^CI27
^FT642,226^A0B,34,33^FH\^CI28^FD{cadena_pedido}^FS^CI27
^PQ1
^XZ
    """
    zpl = zpl_template.format(**datos)
    return zpl

def generar_zpl_faltante(datos):
    """ Genera una etiqueta ZPL con el texto indicado """
    zpl_template = """^XA
^CI28
^FT78,318^A0B,34,41^FH\^CI28^FDNOVELTEX SAS^FS^CI27
^FT330,365^A0B,28,28^FH\^CI28^FDItem^FS^CI27
^FT390,365^A0B,28,28^FH\^CI28^FDRef.^FS^CI27
^FT449,365^A0B,28,28^FH\^CI28^FDColor^FS^CI27
^FT508,365^A0B,28,28^FH\^CI28^FDMetros^FS^CI27
^FT567,365^A0B,28,28^FH\^CI28^FDOperario^FS^CI27
^FT645,381^A0B,39,41^FH\^CI28^FDPEDIDO^FS^CI27
^FO601,22^GB62,227,4^FS
^FT686,395^A0B,20,20^FB395,1,5,C^FH\^CI28^FDconserve esta etiqueta en caso de\\5C&^FS^CI27
^FT711,395^A0B,20,20^FB395,1,5,C^FH\^CI28^FDdevolución o reclamación\\5C&^FS^CI27
^FT758,275^A0B,23,23^FH\^CI28^FD{fecha}^FS^CI27
^FT268,271^A0B,23,23^FH\^CI28^FD{codigo_rollo_hijo}^FS^CI27
^FT112,287^BQN,2,6
^FDQA,{codigo_rollo_hijo}^FS
^FT330,250^A0B,28,28^FH\^CI28^FD{codigo_tela_hijo}^FS^CI27
^FT390,250^A0B,24,24^FH\^CI28^FD{id_refer}^FS^CI27
^FT449,250^A0B,28,28^FH\^CI28^FD{codigo_color_hijo}^FS^CI27
^FT508,250^A0B,28,28^FH\^CI28^FD{longitud_hijo}^FS^CI27
^FT559,250^A0B,20,20^FH\^CI28^FD{nombre_completo}^FS^CI27
^FT642,226^A0B,34,33^FH\^CI28^FDFALTANTE^FS^CI27
^PQ1
^XZ
    """
    zpl = zpl_template.format(**datos)
    return zpl

def generar_zpl_disponible(datos):
    """ Genera una etiqueta ZPL con el texto indicado """
    zpl_template = """^XA
^CI28
^FT78,318^A0B,34,41^FH\^CI28^FDNOVELTEX SAS^FS^CI27
^FT330,365^A0B,28,28^FH\^CI28^FDItem^FS^CI27
^FT390,365^A0B,28,28^FH\^CI28^FDRef.^FS^CI27
^FT449,365^A0B,28,28^FH\^CI28^FDColor^FS^CI27
^FT508,365^A0B,28,28^FH\^CI28^FDMetros^FS^CI27
^FT567,365^A0B,28,28^FH\^CI28^FDOperario^FS^CI27
^FT645,381^A0B,39,41^FH\^CI28^FDPEDIDO^FS^CI27
^FO601,22^GB62,227,4^FS
^FT686,395^A0B,20,20^FB395,1,5,C^FH\^CI28^FDconserve esta etiqueta en caso de\\5C&^FS^CI27
^FT711,395^A0B,20,20^FB395,1,5,C^FH\^CI28^FDdevolución o reclamación\\5C&^FS^CI27
^FT758,275^A0B,23,23^FH\^CI28^FD{fecha}^FS^CI27
^FT268,271^A0B,23,23^FH\^CI28^FD{codigo_rollo_hijo}^FS^CI27
^FT112,287^BQN,2,6
^FDQA,{codigo_rollo_hijo}^FS
^FT330,250^A0B,28,28^FH\^CI28^FD{codigo_tela_hijo}^FS^CI27
^FT390,250^A0B,24,24^FH\^CI28^FD{id_refer}^FS^CI27
^FT449,250^A0B,28,28^FH\^CI28^FD{codigo_color_hijo}^FS^CI27
^FT508,250^A0B,28,28^FH\^CI28^FD{longitud_hijo}^FS^CI27
^FT559,250^A0B,20,20^FH\^CI28^FD{nombre_completo}^FS^CI27
^FT642,226^A0B,34,33^FH\^CI28^FDDISPONIBLE^FS^CI27
^PQ1
^XZ
    """
    zpl = zpl_template.format(**datos)
    return zpl

def generar_zpl_imperfecto(datos):
    """ Genera una etiqueta ZPL con el texto indicado """
    zpl_template = """^XA
^CI28
^FT62,318^A0B,34,41^FH\^CI28^FDNOVELTEX SAS^FS^CI27
^FT330,365^A0B,28,28^FH\^CI28^FDItem^FS^CI27
^FT374,365^A0B,28,28^FH\^CI28^FDRef.^FS^CI27
^FT418,365^A0B,28,28^FH\^CI28^FDColor^FS^CI27
^FT461,365^A0B,28,28^FH\^CI28^FDMetros^FS^CI27
^FT505,365^A0B,28,28^FH\^CI28^FDOperario^FS^CI27
^FT527,398^A0B,17,18^FB393,1,4,C^FH\^CI28^FDconserve esta etiqueta en caso de\\5C&^FS^CI27
^FT548,398^A0B,17,18^FB393,1,4,C^FH\^CI28^FDdevolución o reclamación\\5C&^FS^CI27
^FO696,17^GB93,364,8^FS
^FT682,260^A0B,17,18^FH\^CI28^FDOBSERVACION^FS^CI27
^FT572,334^A0B,17,18^FH\^CI28^FDItem^FS^CI27
^FT572,130^A0B,17,18^FH\^CI28^FDMetros^FS^CI27
^FT86,291^A0B,23,23^FH\^CI28^FD{fecha}^FS^CI27
^FT268,271^A0B,23,23^FH\^CI28^FD{codigo_rollo_hijo}^FS^CI27
^FT112,287^BQN,2,6
^FDQA,{codigo_rollo_hijo}^FS
^FT330,250^A0B,28,28^FH\^CI28^FD{codigo_tela_hijo}^FS^CI27
^FT374,250^A0B,24,24^FH\^CI28^FD{id_refer}^FS^CI27
^FT418,250^A0B,28,28^FH\^CI28^FD{codigo_color_hijo}^FS^CI27
^FT461,250^A0B,28,28^FH\^CI28^FD{longitud_hijo}^FS^CI27
^FT497,250^A0B,20,20^FH\^CI28^FD{nombre_completo}^FS^CI27
^FT581,367^BQN,2,4
^FDQA,{codigo_tela_hijo}^FS
^FT581,155^BQN,2,4
^FDQA,{longitud_hijo}^FS
^FT736,359^A0B,23,23^FH\^CI28^FD{observacion}^FS^CI27
^PQ1
^XZ
    """
    zpl = zpl_template.format(**datos)
    return zpl

def generar_zpl_remedir(datos):
    url_remedir='https://g6344c28e083a44-dbproyectos.adb.us-ashburn-1.oraclecloudapps.com/ords/multiplantas/noveltex/informacion_para_remedir_corte/'
    llave='key001user'
    parametros_rm='/'+datos['codigo_rollo_padre']+'/'+str(datos['id_int_rollo_padre'])+'/'+datos['tipo_orden_padre']+'/'+datos['id_orden_padre']
    print('url rm con parametros: ',url_remedir+llave+parametros_rm)
    
    zpl_template = """^XA
^CI28
^FT78,318^A0B,34,41^FH\^CI28^FDNOVELTEX SAS^FS^CI27
^FT330,365^A0B,24,24^FH\^CI28^FDItem^FS^CI27
^FT390,365^A0B,24,24^FH\^CI28^FDRef.^FS^CI27
^FT449,365^A0B,24,24^FH\^CI28^FDColor^FS^CI27
^FT508,365^A0B,24,24^FH\^CI28^FDMetros^FS^CI27
^FT508,155^A0B,24,24^FH\^CI28^FDSaldo^FS^CI27
^FT567,365^A0B,24,24^FH\^CI28^FDOperario^FS^CI27
^FT645,381^A0B,39,41^FH\^CI28^FDPEDIDO^FS^CI27
^FO601,1^GB62,237,4^FS
^FT685,275^A0B,18,18^FH\^CI28^FD{fecha}^FS^CI27
^FT288,271^A0B,23,23^FH\^CI28^FD{codigo_rollo_padre}-1^FS^CI27
^FT112,297^BQN,2,6
^FDQA,{codigo_rollo_padre}^FS
^FT330,288^A0B,24,24^FH\^CI28^FD{codigo_rollo_padre}^FS^CI27
^FT390,288^A0B,24,24^FH\^CI28^FD{id_refer}^FS^CI27
^FT449,288^A0B,24,24^FH\^CI28^FD{codigo_color_padre}^FS^CI27
^FT508,288^A0B,24,24^FH\^CI28^FD{longitud_padre}^FS^CI27
^FT508,100^A0B,24,24^FH\^CI28^FD{restante}^FS^CI27
^FT564,280^A0B,21,21^FH\^CI28^FD{nombre_completo}^FS^CI27
^FT642,180^A0B,34,33^FH\^CI28^FDREMEDIR^FS^CI27"""
    
    try:
        obtiene_parametros_rm = requests.get(url_remedir+llave+parametros_rm)
        json_data_rm = json.loads(obtiene_parametros_rm.text)
        print('estado consulta interna remedir: ',obtiene_parametros_rm.status_code)
        print('json_data_rm len',len(json_data_rm['items']) )
        
        conteo=0
        pos_y=745
        for fila_rm in json_data_rm['items']:
            print('\nfila_rm ', fila_rm, conteo)
            if(conteo==0):
                zpl_template = zpl_template+"""
                ^FT715,365^A0B,18,18^FH\^CI28^FDFECHA^FS^CI27"""
                zpl_template = zpl_template+"""
                ^FT715,295^A0B,18,18^FH\^CI28^FDSALIDA^FS^CI27"""
                zpl_template = zpl_template+"""
                ^FT715,205^A0B,18,18^FH\^CI28^FDPEDIDO^FS^CI27"""
                zpl_template = zpl_template+"""
                ^FT715,105^A0B,18,18^FH\^CI28^FDOPERARIO^FS^CI27"""
            zpl_template = zpl_template+"""
                ^FT"""+str(pos_y)+""",365^A0B,18,18^FH\^CI28^FD"""+fila_rm['fecha_fix']+"""^FS^CI27
                ^FT"""+str(pos_y)+""",265^A0B,18,18^FH\^CI28^FD"""+str(fila_rm['longitud_hijo'])+"""^FS^CI27
                ^FT"""+str(pos_y)+""",205^A0B,18,18^FH\^CI28^FD"""+fila_rm['tipo_orden_hijo']+fila_rm['id_orden_hijo']+"""^FS^CI27
                ^FT"""+str(pos_y)+""",105^A0B,10,10^FH\^CI28^FD"""+fila_rm['nombre_completo']+"""^FS^CI27"""
            pos_y=pos_y+25
            conteo=conteo+1
    except Exception as e:
            print(f"❌ Error Remedir segundo ws: {e}")
    zpl_template = zpl_template+"""
^PQ1
^XZ
    """
    zpl = zpl_template.format(**datos)
    return zpl

def imprime_pendientes(imprime_flag=True, permite_desechar=True):  
    url='https://g6344c28e083a44-dbproyectos.adb.us-ashburn-1.oraclecloudapps.com/ords/multiplantas/noveltex/pendientes_impresion_corte/'
    llave='key001user'
    
    obtiene_parametros = requests.get(url+llave)
    json_data = json.loads(obtiene_parametros.text)
    print(obtiene_parametros.status_code)
    print('json_data len',len(json_data['items']) )

    ultima_llave_rollo=''
    
    for fila in json_data['items']:
        print('\nfila ', fila)
        tipo_impresion=fila['tipo_impresion']
        llave_rollo=fila['llave_fila']
        id_maquina=fila['id_maquina']
        
        # --- MODIFICADO: Prioridad a las variables de entorno de Render ---
        IP_IMPRESORA = os.getenv("ZEBRA_IP") or fila.get('url_impresora') or "192.168.1.40"
        
        env_port = os.getenv("ZEBRA_PORT")
        if env_port:
            PUERTO_ZEBRA = int(env_port)
        else:
            PUERTO_ZEBRA = int(fila.get('puerto_impresora', 9100) or 9100)
        # ------------------------------------------------------------------
        
        codigo_rollo = fila.get("codigo_rollo_hijo", "N/A")
        print('\ntipo_impresion',tipo_impresion,'llave_rollo',llave_rollo,'id_maquina',id_maquina,'codigo_rollo',codigo_rollo)
        
        COMANDO_ZPL = ''
        if(tipo_impresion=='NORMAL'):
            print('Entro a impresion NORMAL')
            COMANDO_ZPL = generar_zpl_normal(fila)
        elif(tipo_impresion=='IMPERFECTO'):
            print('Entro a impresion IMPERFECTO')
            COMANDO_ZPL = generar_zpl_imperfecto(fila)
        elif(tipo_impresion=='FALTANTE'):
            print('Entro a impresion FALTANTE')
            COMANDO_ZPL = generar_zpl_faltante(fila)
        elif(tipo_impresion=='DISPONIBLE'):
            print('Entro a impresion DISPONIBLE')
            COMANDO_ZPL = generar_zpl_disponible(fila)
        elif(tipo_impresion=='REMEDIR'):
            print('Entro a impresion REMEDIR')
            COMANDO_ZPL = generar_zpl_remedir(fila)
        
        if(COMANDO_ZPL!=''):
            print('COMANDO_ZPL',COMANDO_ZPL)            
            if(imprime_flag==True and ultima_llave_rollo!=llave_rollo):
                imprimir_zebra(IP_IMPRESORA, PUERTO_ZEBRA, COMANDO_ZPL)
                ultima_llave_rollo=llave_rollo
            
        llave='key001user'
        rowid_encoded = urllib.parse.quote(llave_rollo)
        
        url_post='https://g6344c28e083a44-dbproyectos.adb.us-ashburn-1.oraclecloudapps.com/ords/multiplantas/noveltex/remueve_de_impresion_corte/'
        if(imprime_flag==True or permite_desechar==True):
            print('se retira rollo ',fila['codigo_rollo_hijo'],'llave ',llave_rollo,'rowid_encoded',rowid_encoded)
            print('url corte',url_post+llave+'/'+rowid_encoded)
            r_post = requests.post(url_post+llave+'/'+rowid_encoded )
            print('r_post.status_code', r_post.status_code)

def generar_zpl_contado(datos):
    """ Genera una etiqueta ZPL con el texto indicado """
    zpl_template = """^XA
^CI28
^FT70,280^A0B,34,41^FH\^CI28^FDCONTADO^FS^CI27
^FT95,260^A0B,18,18^FH\^CI28^FDNOVELTEX SAS^FS^CI27
^FT440,390^BQN,2,5
^FDQA,{url_qr}^FS
^FT120,390^A0B,20,20^FH\^CI28^FDFECHA^FS^CI27
^FT145,390^A0B,20,20^FH\^CI28^FDFECHA VENC.^FS^CI27
^FT190,390^A0B,20,20^FH\^CI28^FDCANTIDAD SOLICITADA^FS^CI27
^FO195,140^GB70,250,4^FS
^FT305,390^A0B,39,41^FH\^CI28^FDPEDIDO^FS^CI27
^FO270,50^GB50,200,4^FS
^FT380,390^A0B,20,20^FH\^CI28^FDOPERARIO:^FS^CI27
^FT405,390^A0B,20,20^FH\^CI28^FD{nombre_operario}^FS^CI27
^FT120,270^A0B,20,20^FH\^CI28^FD{fecha_registro_contado}^FS^CI27
^FT145,270^A0B,20,20^FH\^CI28^FD{fecha_vencimiento}^FS^CI27
^FT305,240^A0B,34,33^FH\^CI28^FD{cadena_pedido}^FS^CI27
^PQ1
^XZ
    """
    zpl = zpl_template.format(**datos)
    return zpl

def imprime_pendientes_contado(imprime_flag=True, permite_desechar=True):  
    url='https://g6344c28e083a44-dbproyectos.adb.us-ashburn-1.oraclecloudapps.com/ords/multiplantas/noveltex/para_imprimir_en_contado/'
    llave='key001user'
    
    obtiene_parametros = requests.get(url+llave)
    json_data = json.loads(obtiene_parametros.text)
    print(obtiene_parametros.status_code)
    print('json_data len',len(json_data['items']) )

    ultima_llave_rollo=''
    
    for fila in json_data['items']:
        print('\nfila ', fila)
        tipo_impresion=fila['tipo_impresion']
        llave_rollo=fila['llave_fila']
        id_maquina=fila['id_maquina']
        
        # --- MODIFICADO: Prioridad a las variables de entorno de Render ---
        IP_IMPRESORA = os.getenv("ZEBRA_IP") or fila.get('url_impresora') or "192.168.1.40"
        
        env_port = os.getenv("ZEBRA_PORT")
        if env_port:
            PUERTO_ZEBRA = int(env_port)
        else:
            PUERTO_ZEBRA = int(fila.get('puerto_impresora', 9100) or 9100)
        # ------------------------------------------------------------------
        
        numero_impresiones=fila['numero_impresiones']
        print('numero_impresiones',numero_impresiones)
        print('\ntipo_impresion',tipo_impresion,'llave_rollo',llave_rollo,'id_maquina',id_maquina)
        
        while numero_impresiones > 0 and ultima_llave_rollo!=llave_rollo:
            print(f"Impresión número: {numero_impresiones}")
            
            COMANDO_ZPL = generar_zpl_contado(fila)        
            
            if(COMANDO_ZPL!=''):
                print('COMANDO_ZPL',COMANDO_ZPL)            
                if(imprime_flag==True):
                    imprimir_zebra(IP_IMPRESORA, PUERTO_ZEBRA, COMANDO_ZPL)
                    
            llave='key001user'
            rowid_encoded = urllib.parse.quote(llave_rollo)
            
            url_post='https://g6344c28e083a44-dbproyectos.adb.us-ashburn-1.oraclecloudapps.com/ords/multiplantas/noveltex/remueve_de_impresion_contado/'
            if(imprime_flag==True or permite_desechar==True):
                print('se retira pedido contado ',fila['cadena_pedido'],fila['numero_contado'],'llave ',llave_rollo,'rowid_encoded',rowid_encoded)
                print('url contado',url_post+llave+'/'+rowid_encoded)
                r_post = requests.post(url_post+llave+'/'+rowid_encoded )
                print('r_post.status_code', r_post.status_code)
            
            numero_impresiones -= 1
        ultima_llave_rollo=llave_rollo

def main():
    while True:
        try:
            print('VERIFICA E IMPRIME PENDIENTES CORTE NORMAL E IMPERFECTO')
            imprime_pendientes()
        except Exception as e:
            print(f"❌ Error Etiquetas Corte: {e}")
        time.sleep(5)
        try:
            print('VERIFICA E IMPRIME PENDIENTES CONTADO')                        
            imprime_pendientes_contado()
        except Exception as e:
            print(f"❌ Error Etiquetas Contado: {e}")
        segundos_duerme=15
        print('duerme ',segundos_duerme,' segundos')
        time.sleep(segundos_duerme)

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Script para prueba local de los diferentes métodos de remover fondo.
Descarga una imagen de ejemplo y compara velocidad y resultados.
"""

import asyncio
import time
from PIL import Image
from app.utils.download import download_image
from app.services.background_removal_service import apply_background_removal


async def test_background_removal():
    # URL de ejemplo - usar una imagen simple de prueba
    test_urls = [
        "https://www.haveparkdele.dk/images/maerker/briggs/271256.jpg",
    ]
    
    print("=" * 80)
    print("PRUEBA DE MÉTODOS DE REMOVER FONDO")
    print("=" * 80)
    
    methods = ["rembg", "watershed", "threshold_simple", "laplacian_edge"]
    
    for url in test_urls:
        print(f"\n📥 Descargando: {url[:60]}...")
        
        try:
            img = await download_image(url)
            print(f"   ✓ Imagen cargada: {img.size} píxeles, modo {img.mode}")
            
            results = {}
            
            for method in methods:
                print(f"\n   Probando método: {method}")
                print(f"   {'─' * 50}")
                
                try:
                    start = time.time()
                    result_img = apply_background_removal(img.copy(), method=method)
                    elapsed = time.time() - start
                    
                    results[method] = {
                        "status": "✓ OK",
                        "time": elapsed,
                        "size": result_img.size,
                        "mode": result_img.mode
                    }
                    
                    # Guardar resultado
                    filename = f"test_output_{method}.png"
                    result_img.save(filename)
                    
                    print(f"   ✓ Status: {results[method]['status']}")
                    print(f"   ✓ Tiempo: {elapsed:.3f} segundos")
                    print(f"   ✓ Tamaño: {results[method]['size']}")
                    print(f"   ✓ Archivo guardado: {filename}")
                    
                except Exception as e:
                    results[method] = {
                        "status": f"✗ ERROR: {str(e)}",
                        "time": None
                    }
                    print(f"   ✗ Error: {str(e)}")
            
            # Resumen
            print("\n" + "=" * 80)
            print("RESUMEN DE RESULTADOS")
            print("=" * 80)
            
            for method in methods:
                if results[method]["time"]:
                    print(f"  {method:20} | {results[method]['time']:8.3f}s | {results[method]['status']}")
                else:
                    print(f"  {method:20} | {'N/A':8} | {results[method]['status']}")
            
            # Recomendación
            valid_results = {m: r["time"] for m, r in results.items() if r["time"]}
            if valid_results:
                fastest = min(valid_results, key=valid_results.get)
                slowest = max(valid_results, key=valid_results.get)
                
                print("\n" + "─" * 80)
                print(f"⚡ Más rápido: {fastest} ({valid_results[fastest]:.3f}s)")
                print(f"🐢 Más lento: {slowest} ({valid_results[slowest]:.3f}s)")
                print(f"✨ RECOMENDADO: rembg (mejor calidad)")
                print("─" * 80)
        
        except Exception as e:
            print(f"   ✗ Error descargando imagen: {str(e)}")
            print(f"   Intenta con una URL diferente o una imagen local")


if __name__ == "__main__":
    asyncio.run(test_background_removal())

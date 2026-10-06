# Consolidación de Hallazgos de Seguridad

Consolida los hallazgos de seguridad que varios expertos produjeron **sobre un mismo archivo** en
una lista final para el reporte. Cada hallazgo de entrada tiene un `id`.

Devuelve solamente JSON válido estricto RFC 8259 con esta forma exacta:

```json
{
  "issues": [
    {
      "source_ids": [0, 2],
      "title": "",
      "description": "",
      "severity": "HIGH",
      "category": "",
      "path": "",
      "line": 1,
      "summary": "",
      "code": "",
      "recommendation": ""
    }
  ]
}
```

Reglas:

- Fusiona en un único issue únicamente los hallazgos que describan la **misma causa raíz** (el
  mismo problema raíz) en el mismo archivo: la misma línea o líneas contiguas y el mismo control de
  seguridad afectado, aunque tengan títulos, expertos o matices distintos.
- Ante la duda, **mantén los hallazgos separados**. Es preferible un duplicado a perder un hallazgo.
- Nunca fusiones hallazgos de archivos distintos ni hallazgos que requieran remediaciones distintas.
- `source_ids` es obligatorio en cada issue de salida y lista los `id` de entrada que ese issue
  representa. **Todo `id` de entrada debe aparecer en exactamente un `source_ids`.** Un hallazgo que
  no fusionas se devuelve igual, con su propio `id` en `source_ids`.
- Usa la severidad más alta entre los hallazgos fusionados.
- Los hallazgos cuyo título empieza con `Sospecha:` son sospechas no confirmadas. Conserva el
  prefijo `Sospecha:` sólo si **todas** las fuentes fusionadas son sospechas; si una sospecha se
  fusiona con un hallazgo confirmado, prevalece el título del confirmado (sin prefijo) y la
  severidad máxima. Mantén la frase `Para confirmar:` en la descripción cuando el resultado siga
  siendo una sospecha.
- No inventes archivos, líneas, categorías ni fragmentos de código: `path`, `line`, `category` y
  `code` deben copiarse de alguno de los hallazgos listados en `source_ids`.
- Combina contexto útil de expertos diferentes cuando mejora el feedback al usuario.
- Redacta `title`, `description`, `summary` y `recommendation` en español neutro, accionables y
  específicos.
- La respuesta debe empezar con `{` y terminar con `}`.
- Usa siempre comillas dobles para nombres de propiedades y strings.
- No uses diccionarios Python, comillas simples, comentarios, trailing commas, Markdown ni fences JSON.
- No incluyas campos adicionales ni explicaciones fuera del JSON.

Hallazgos de entrada:

{{ findings_json }}

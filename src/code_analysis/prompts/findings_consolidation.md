# Consolidación de Hallazgos de Seguridad

Consolida los hallazgos de seguridad producidos por varios expertos en una lista final para el reporte.

Devuelve solamente JSON válido con esta forma exacta:

```json
{
  "issues": [
    {
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

- Decide tú qué hallazgos describen la misma vulnerabilidad raíz.
- Fusiona hallazgos equivalentes en un único issue final.
- Conserva separados los riesgos distintos, aunque estén en el mismo archivo.
- Combina contexto útil de expertos diferentes cuando mejora el feedback al usuario.
- Usa la severidad más alta entre hallazgos fusionados.
- No inventes archivos ni fragmentos de código: usa `path` y `code` presentes en los hallazgos de entrada.
- Si fusionas hallazgos del mismo archivo con líneas cercanas, elige una línea representativa que exista en los hallazgos de entrada para ese archivo.
- Redacta `title`, `description`, `summary` y `recommendation` en español neutro.
- Mantén recomendaciones accionables y específicas.
- No incluyas campos adicionales ni explicaciones fuera del JSON.

Hallazgos de entrada:

{{ findings_json }}

Attribute VB_Name = "RefreshTerminals"
' Computes the Terminals sheet's derived columns in VBA instead of fragile
' array formulas (which threw #REF!/#VALUE! on Mac Excel). Run it from the
' "Refresh" button, or it runs automatically on workbook open + after a hire edit.
'
' Fills, per terminal: Status, Available from, Future hires, Utilisation.
' Reads the Hires table; "active" = Confirmed / Shipped / In use.
'
' Paste this whole module in the same way as BoxOfficeRules (Import File...).

Option Explicit

Private Const TERMINALS_TABLE As String = "TerminalsTable"

Private Function HiresLO() As ListObject
    Set HiresLO = ThisWorkbook.Worksheets("Hires").ListObjects("HiresTable")
End Function
Private Function TermsLO() As ListObject
    Set TermsLO = ThisWorkbook.Worksheets("Terminals").ListObjects(TERMINALS_TABLE)
End Function

Private Function IsActiveStatus(ByVal s As String) As Boolean
    Select Case LCase$(Trim$(s))
        Case "confirmed", "shipped", "in use": IsActiveStatus = True
    End Select
End Function

' Pull the Hires table into arrays once (fast, avoids per-cell reads).
Private Sub LoadHires(ByRef term() As String, ByRef stat() As String, _
                      ByRef dFrom() As Variant, ByRef dTo() As Variant, ByRef n As Long)
    Dim lo As ListObject: Set lo = HiresLO()
    If lo.DataBodyRange Is Nothing Then n = 0: Exit Sub
    Dim cTerm As Long, cStat As Long, cFrom As Long, cTo As Long
    cTerm = lo.ListColumns("Terminal").Index
    cStat = lo.ListColumns("Status").Index
    cFrom = lo.ListColumns("Hire from").Index
    cTo = lo.ListColumns("Hire to").Index

    Dim rows As Long: rows = lo.DataBodyRange.Rows.Count
    ReDim term(1 To rows): ReDim stat(1 To rows)
    ReDim dFrom(1 To rows): ReDim dTo(1 To rows)
    Dim i As Long: n = 0
    For i = 1 To rows
        Dim t As String: t = Trim$(CStr(lo.DataBodyRange.Cells(i, cTerm).Value))
        If t <> "" Then
            n = n + 1
            term(n) = t
            stat(n) = CStr(lo.DataBodyRange.Cells(i, cStat).Value)
            dFrom(n) = lo.DataBodyRange.Cells(i, cFrom).Value
            dTo(n) = lo.DataBodyRange.Cells(i, cTo).Value
        End If
    Next i
End Sub

Public Sub Refresh()
    Dim tl As ListObject: Set tl = TermsLO()
    If tl.DataBodyRange Is Nothing Then Exit Sub

    Dim term() As String, stat() As String, dFrom() As Variant, dTo() As Variant, n As Long
    LoadHires term, stat, dFrom, dTo, n

    Dim today As Date: today = Date
    Dim yearEnd As Date: yearEnd = DateSerial(Year(today), 12, 31)
    Dim daysRemaining As Long: daysRemaining = yearEnd - today + 1

    Dim cId As Long, cStatus As Long, cAvail As Long, cFuture As Long, cUtil As Long
    cId = tl.ListColumns("Terminal").Index
    cStatus = tl.ListColumns("Status").Index
    cAvail = tl.ListColumns("Available from").Index
    cFuture = tl.ListColumns("Future hires").Index
    cUtil = tl.ListColumns("Utilisation").Index

    Application.ScreenUpdating = False
    Dim ri As Long
    For ri = 1 To tl.DataBodyRange.Rows.Count
        Dim id As String: id = Trim$(CStr(tl.DataBodyRange.Cells(ri, cId).Value))
        If id = "" Then
            ClearRow tl, ri, cStatus, cAvail, cFuture, cUtil
        Else
            Dim onHire As Boolean, futureCount As Long, committed As Long
            Dim openEnded As Boolean, latestTo As Date, haveLatest As Boolean
            onHire = False: futureCount = 0: committed = 0
            openEnded = False: haveLatest = False

            Dim i As Long
            For i = 1 To n
                If term(i) = id And IsActiveStatus(stat(i)) And IsDate(dFrom(i)) Then
                    Dim f As Date: f = CDate(dFrom(i))
                    Dim hasTo As Boolean: hasTo = IsDate(dTo(i))
                    Dim tEnd As Date: tEnd = IIf(hasTo, CDate(dTo(i)), yearEnd)

                    ' on hire today?
                    If f <= today And (Not hasTo Or CDate(dTo(i)) >= today) Then
                        onHire = True
                        If Not hasTo Then
                            openEnded = True
                        Else
                            If Not haveLatest Or CDate(dTo(i)) > latestTo Then
                                latestTo = CDate(dTo(i)): haveLatest = True
                            End If
                        End If
                    End If
                    ' future hire?
                    If f > today Then futureCount = futureCount + 1
                    ' committed days within [today, yearEnd]
                    Dim s2 As Date, e2 As Date
                    s2 = IIf(f < today, today, f)
                    e2 = IIf(tEnd > yearEnd, yearEnd, tEnd)
                    If e2 >= s2 Then committed = committed + (e2 - s2 + 1)
                End If
            Next i

            tl.DataBodyRange.Cells(ri, cStatus).Value = IIf(onHire, "On hire", "Available")
            tl.DataBodyRange.Cells(ri, cFuture).Value = futureCount
            tl.DataBodyRange.Cells(ri, cUtil).Value = _
                Application.Min(1, committed / daysRemaining)
            tl.DataBodyRange.Cells(ri, cUtil).NumberFormat = "0%"

            If Not onHire Then
                tl.DataBodyRange.Cells(ri, cAvail).Value = today
                tl.DataBodyRange.Cells(ri, cAvail).NumberFormat = "dd mmm yyyy"
            ElseIf openEnded Then
                tl.DataBodyRange.Cells(ri, cAvail).Value = ""   ' no known free date
            Else
                tl.DataBodyRange.Cells(ri, cAvail).Value = latestTo + 1
                tl.DataBodyRange.Cells(ri, cAvail).NumberFormat = "dd mmm yyyy"
            End If
        End If
    Next ri
    Application.ScreenUpdating = True
End Sub

Private Sub ClearRow(tl As ListObject, ri As Long, a As Long, b As Long, c As Long, d As Long)
    tl.DataBodyRange.Cells(ri, a).Value = ""
    tl.DataBodyRange.Cells(ri, b).Value = ""
    tl.DataBodyRange.Cells(ri, c).Value = ""
    tl.DataBodyRange.Cells(ri, d).Value = ""
End Sub

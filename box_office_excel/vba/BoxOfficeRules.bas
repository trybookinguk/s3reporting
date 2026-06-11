Attribute VB_Name = "BoxOfficeRules"
' Box Office Terminals - business rules (the bits a formula can't enforce).
'
' Ported from the reporting-dashboard box-office feature:
'   - double-booking guard: a terminal can't be on two overlapping ACTIVE hires,
'     UNLESS one continues from the other (the kit never came back).
'   - continuation: when "Continues from (ref)" is set, the prior hire is marked
'     'completed' and contact/address/dates carry over.
'   - the Trial tick bypasses the payment-required rule.
'
' Design for Excel: ONE terminal per hire row. Single-owner editing.
' Tick columns hold the character below (TICK) for yes, blank for no.
'
' Column headers below MUST match build_workbook.py exactly. If you rename a
' header in the workbook, update the matching constant here.
'
' Paste this whole module into the VBA editor (Alt+F11 -> right-click
' VBAProject -> Import File... -> BoxOfficeRules.bas), then add the small
' Worksheet_Change hook to the Hires sheet's code (see the .txt file).

Option Explicit

Public Const HIRES_TABLE As String = "HiresTable"

' --- column headers (friendly names from the workbook) --------------------
Private Const COL_REF As String = "Ref"
Private Const COL_ACCT_REF As String = "Account ref"
Private Const COL_ACCOUNT As String = "Account"
Private Const COL_CONTACT_NAME As String = "Contact name"
Private Const COL_CONTACT_EMAIL As String = "Contact email"
Private Const COL_CONTACT_PHONE As String = "Contact phone"
Private Const COL_STATUS As String = "Status"
Private Const COL_TRIAL As String = "Trial"
Private Const COL_FROM As String = "Hire from"
Private Const COL_TO As String = "Hire to"
Private Const COL_TERMINAL As String = "Terminal"
Private Const COL_SEND_OUT As String = "Send out by"
Private Const COL_ADDRESS As String = "Shipping address"
Private Const COL_PAID As String = "Paid"
Private Const COL_CONTINUES As String = "Continues from (ref)"
Private Const COL_CHANGED_BY As String = "Changed by"
Private Const COL_CHANGED_AT As String = "Changed at"

Public Const CONTINUES_HEADER As String = COL_CONTINUES   ' used by the sheet hook

' --- status helpers (work in the human-readable labels shown in the sheet) --
Private Function IsActive(ByVal status As String) As Boolean
    Select Case LCase$(Trim$(status))
        Case "confirmed", "shipped", "in use": IsActive = True
        Case Else: IsActive = False
    End Select
End Function

Private Function IsEnded(ByVal status As String) As Boolean
    Select Case LCase$(Trim$(status))
        Case "returned", "completed", "cancelled": IsEnded = True
        Case Else: IsEnded = False
    End Select
End Function

' Tick columns hold TRUE/FALSE (native checkboxes). Treat blanks as FALSE.
Private Function IsTicked(ByVal v As Variant) As Boolean
    On Error Resume Next
    IsTicked = CBool(v)
    On Error GoTo 0
End Function

' --- table access helpers -------------------------------------------------
Private Function HiresLO() As ListObject
    Set HiresLO = ThisWorkbook.Worksheets("Hires").ListObjects(HIRES_TABLE)
End Function

Private Function ColIndex(lo As ListObject, ByVal header As String) As Long
    ColIndex = lo.ListColumns(header).Index
End Function

Private Function CellVal(r As Range, lo As ListObject, ByVal header As String) As Variant
    CellVal = r.Cells(1, ColIndex(lo, header)).Value
End Function

' Whole-day overlap of [aFrom,aTo] and [bFrom,bTo]; empty 'to' = open-ended.
Private Function RangesOverlap(aFrom As Date, aTo As Variant, _
                               bFrom As Date, bTo As Variant) As Boolean
    Dim aEnd As Date, bEnd As Date
    aEnd = IIf(IsDate(aTo), CDate(aTo), DateSerial(9999, 12, 31))
    bEnd = IIf(IsDate(bTo), CDate(bTo), DateSerial(9999, 12, 31))
    RangesOverlap = (aFrom <= bEnd) And (bFrom <= aEnd)
End Function

' --- the double-booking + payment guard -----------------------------------
' Returns "" if OK, or an error message describing why the row is rejected.
Public Function ValidateHireRow(ByVal rowIndex As Long) As String
    Dim lo As ListObject: Set lo = HiresLO()
    Dim r As Range: Set r = lo.DataBodyRange.Rows(rowIndex)

    Dim myId As String, myStatus As String, myTerm As String, myCont As String
    myId = CStr(CellVal(r, lo, COL_REF))
    myStatus = CStr(CellVal(r, lo, COL_STATUS))
    myTerm = CStr(CellVal(r, lo, COL_TERMINAL))
    myCont = CStr(CellVal(r, lo, COL_CONTINUES))

    If Trim$(myTerm) = "" Then Exit Function          ' no terminal yet
    If Not IsActive(myStatus) Then Exit Function       ' only active hires clash

    If Not IsDate(CellVal(r, lo, COL_FROM)) Then
        ValidateHireRow = "'" & COL_FROM & "' must be a date."
        Exit Function
    End If
    Dim myFrom As Date: myFrom = CDate(CellVal(r, lo, COL_FROM))
    Dim myTo As Variant: myTo = CellVal(r, lo, COL_TO)

    Dim i As Long, other As Range
    For i = 1 To lo.DataBodyRange.Rows.Count
        If i <> rowIndex Then
            Set other = lo.DataBodyRange.Rows(i)
            Dim oId As String, oTerm As String, oStatus As String, oCont As String
            oId = CStr(CellVal(other, lo, COL_REF))
            oTerm = CStr(CellVal(other, lo, COL_TERMINAL))
            oStatus = CStr(CellVal(other, lo, COL_STATUS))
            oCont = CStr(CellVal(other, lo, COL_CONTINUES))

            If oTerm = myTerm And IsActive(oStatus) Then
                If Not (oId = myCont Or myId = oCont) Then    ' skip linked partner
                    If IsDate(CellVal(other, lo, COL_FROM)) Then
                        If RangesOverlap(myFrom, myTo, _
                                         CDate(CellVal(other, lo, COL_FROM)), _
                                         CellVal(other, lo, COL_TO)) Then
                            ValidateHireRow = "Terminal '" & myTerm & _
                                "' is already on an active, overlapping hire (" & _
                                CStr(CellVal(other, lo, COL_ACCOUNT)) & _
                                "). Link it as a continuation if the kit stayed out."
                            Exit Function
                        End If
                    End If
                End If
            End If
        End If
    Next i

    ' Trial bypasses payment; otherwise an active hire needs the Paid tick.
    If Not IsTicked(CellVal(r, lo, COL_TRIAL)) Then
        If Not IsTicked(CellVal(r, lo, COL_PAID)) Then
            ValidateHireRow = "Status '" & myStatus & _
                "' needs the '" & COL_PAID & "' tick (or tick '" & COL_TRIAL & "')."
        End If
    End If
End Function

' --- continuation: complete the prior hire + carry details over -----------
Public Sub ApplyContinuationForRow(ByVal rowIndex As Long)
    Dim lo As ListObject: Set lo = HiresLO()
    Dim r As Range: Set r = lo.DataBodyRange.Rows(rowIndex)
    Dim priorId As String: priorId = CStr(CellVal(r, lo, COL_CONTINUES))
    If Trim$(priorId) = "" Then Exit Sub

    Dim i As Long, prior As Range
    For i = 1 To lo.DataBodyRange.Rows.Count
        If CStr(CellVal(lo.DataBodyRange.Rows(i), lo, COL_REF)) = priorId Then
            Set prior = lo.DataBodyRange.Rows(i)
            Exit For
        End If
    Next i
    If prior Is Nothing Then Exit Sub

    ' Carry over contact + address; outbound = collect (already on site).
    CopyCol prior, r, lo, COL_CONTACT_NAME
    CopyCol prior, r, lo, COL_CONTACT_EMAIL
    CopyCol prior, r, lo, COL_CONTACT_PHONE
    CopyCol prior, r, lo, COL_ADDRESS
    CopyCol prior, r, lo, COL_ACCT_REF
    CopyCol prior, r, lo, COL_ACCOUNT
    r.Cells(1, ColIndex(lo, COL_SEND_OUT)).Value = "Collect"

    ' Start the day after the prior ends (or today if open-ended).
    Dim pTo As Variant: pTo = CellVal(prior, lo, COL_TO)
    Dim newFrom As Date: newFrom = IIf(IsDate(pTo), CDate(pTo) + 1, Date)
    r.Cells(1, ColIndex(lo, COL_FROM)).Value = newFrom
    Dim myTo As Variant: myTo = CellVal(r, lo, COL_TO)
    If IsDate(myTo) Then
        If CDate(myTo) < newFrom Then r.Cells(1, ColIndex(lo, COL_TO)).Value = ""
    End If

    ' Close out the prior hire unless already ended.
    If Not IsEnded(CStr(CellVal(prior, lo, COL_STATUS))) Then
        prior.Cells(1, ColIndex(lo, COL_STATUS)).Value = "Completed"
        prior.Cells(1, ColIndex(lo, COL_CHANGED_AT)).Value = Now
    End If
End Sub

Private Sub CopyCol(src As Range, dst As Range, lo As ListObject, ByVal header As String)
    Dim c As Long: c = ColIndex(lo, header)
    dst.Cells(1, c).Value = src.Cells(1, c).Value
End Sub

' --- stamp a new Ref / audit on a fresh row -------------------------------
Public Sub StampRow(ByVal rowIndex As Long)
    Dim lo As ListObject: Set lo = HiresLO()
    Dim r As Range: Set r = lo.DataBodyRange.Rows(rowIndex)
    Dim cRef As Long: cRef = ColIndex(lo, COL_REF)
    If Trim$(CStr(r.Cells(1, cRef).Value)) = "" Then
        r.Cells(1, cRef).Value = "H" & Format(Now, "yyyymmddhhnnss") & _
                                 Right$("000" & rowIndex, 3)
    End If
    r.Cells(1, ColIndex(lo, COL_CHANGED_AT)).Value = Now
    If Trim$(CStr(r.Cells(1, ColIndex(lo, COL_CHANGED_BY)).Value)) = "" Then
        r.Cells(1, ColIndex(lo, COL_CHANGED_BY)).Value = Environ$("Username")
    End If
End Sub
